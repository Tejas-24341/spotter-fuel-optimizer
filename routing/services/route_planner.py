"""Orchestrates geocode → route → station filter → optimize."""

from __future__ import annotations

import copy
import hashlib
import logging
import re
import time
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache

from routing.services.geo import simplify_route_coordinates
from routing.services.geocoding import (
    GeocodingError,
    GeocodingUpstreamError,
    geocode,
    normalize_query,
)
from routing.services.optimizer import UnreachableRouteError, optimize_fuel_stops
from routing.services.routing_client import (
    RoutingError,
    RoutingUpstreamError,
    fetch_route,
)
from routing.services.stations import stations_near_route

logger = logging.getLogger(__name__)


class RoutePlanError(Exception):
    """Domain error with an HTTP-ish status hint."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _norm_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _plan_cache_key(
    start: str,
    finish: str,
    starting_fuel_gallons: float,
    min_purchase_gallons: float,
    reserve_miles: float,
) -> str:
    raw = (
        f"{normalize_query(start)}|{normalize_query(finish)}|"
        f"{starting_fuel_gallons:.4f}|{min_purchase_gallons:.4f}|{reserve_miles:.4f}"
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"plan:v4:{digest}"


def plan_route(
    start: str,
    finish: str,
    *,
    starting_fuel_gallons: float | None = None,
) -> dict[str, Any]:
    """
    Build the full route + fuel-stop response payload.

    Caches the complete plan by normalized start/finish/fuel so warm repeats
    avoid re-filtering stations. Logs timing per stage.
    """
    start = _norm_spaces(start)
    finish = _norm_spaces(finish)
    max_gal = float(settings.VEHICLE_MAX_RANGE_MILES) / float(settings.VEHICLE_MPG)
    if starting_fuel_gallons is None:
        starting_fuel_gallons = max_gal
    starting_fuel_gallons = float(starting_fuel_gallons)
    min_purchase_gallons = float(settings.MIN_PURCHASE_GALLONS)
    reserve_miles = float(settings.RESERVE_MILES)

    cache_key = _plan_cache_key(
        start,
        finish,
        starting_fuel_gallons,
        min_purchase_gallons,
        reserve_miles,
    )
    cached = cache.get(cache_key)
    if cached is not None:
        payload = copy.deepcopy(cached)
        payload["timings_ms"] = {
            "geocode_ms": 0.0,
            "routing_ms": 0.0,
            "stations_ms": 0.0,
            "optimize_ms": 0.0,
            "total_ms": 0.0,
            "cache": "hit",
        }
        logger.info(
            "plan_route cache hit for %r → %r (fuel=%.2f)",
            start,
            finish,
            starting_fuel_gallons,
        )
        return payload

    timings: dict[str, float] = {}

    t0 = time.perf_counter()
    try:
        start_pt = geocode(start)
        finish_pt = geocode(finish)
    except GeocodingUpstreamError as exc:
        raise RoutePlanError(str(exc), status_code=exc.status_code) from exc
    except GeocodingError as exc:
        raise RoutePlanError(str(exc), status_code=400) from exc
    timings["geocode_ms"] = (time.perf_counter() - t0) * 1000

    if start_pt.lat == finish_pt.lat and start_pt.lng == finish_pt.lng:
        raise RoutePlanError(
            "Start and finish resolve to the same location.", status_code=400
        )

    t1 = time.perf_counter()
    try:
        route = fetch_route(start_pt.lat, start_pt.lng, finish_pt.lat, finish_pt.lng)
    except RoutingUpstreamError as exc:
        raise RoutePlanError(str(exc), status_code=exc.status_code) from exc
    except RoutingError as exc:
        raise RoutePlanError(str(exc), status_code=400) from exc
    timings["routing_ms"] = (time.perf_counter() - t1) * 1000

    t2 = time.perf_counter()
    candidates = stations_near_route(route.coordinates)
    timings["stations_ms"] = (time.perf_counter() - t2) * 1000

    t3 = time.perf_counter()
    try:
        result = optimize_fuel_stops(
            candidates,
            route.distance_miles,
            max_range_miles=float(settings.VEHICLE_MAX_RANGE_MILES),
            mpg=float(settings.VEHICLE_MPG),
            starting_fuel_gallons=starting_fuel_gallons,
            min_purchase_gallons=min_purchase_gallons,
            reserve_miles=reserve_miles,
        )
    except UnreachableRouteError as exc:
        raise RoutePlanError(str(exc), status_code=400) from exc
    timings["optimize_ms"] = (time.perf_counter() - t3) * 1000
    timings["total_ms"] = sum(timings.values())

    logger.info(
        "plan_route timings: geocode=%.1fms routing=%.1fms stations=%.1fms "
        "optimize=%.1fms total=%.1fms stops=%d distance=%.1fmi",
        timings["geocode_ms"],
        timings["routing_ms"],
        timings["stations_ms"],
        timings["optimize_ms"],
        timings["total_ms"],
        len(result.stops),
        route.distance_miles,
    )

    fuel_stops = [
        {
            "name": p.station.name,
            "city": p.station.city,
            "state": p.station.state,
            "lat": round(p.station.lat, 5),
            "lng": round(p.station.lng, 5),
            "mile_marker": round(p.station.mile_marker, 2),
            "price_per_gallon": round(p.station.price, 2),
            "gallons_purchased": round(p.gallons_purchased, 2),
            "cost_usd": round(p.cost_usd, 2),
        }
        for p in result.stops
    ]

    qs = urlencode(
        {
            "start": start,
            "finish": finish,
            "starting_fuel_gallons": f"{starting_fuel_gallons:.4g}",
        }
    )
    payload = {
        "start": start_pt.to_dict(),
        "finish": finish_pt.to_dict(),
        "total_distance_miles": round(route.distance_miles, 2),
        "total_fuel_cost_usd": round(result.total_cost_usd, 2),
        "total_gallons": round(result.total_gallons, 2),
        "fuel_stops": fuel_stops,
        "route": {
            "type": "LineString",
            "coordinates": simplify_route_coordinates(route.coordinates),
        },
        "map_url": f"/api/route/map/?{qs}",
        "assumptions": {
            "max_range_miles": float(settings.VEHICLE_MAX_RANGE_MILES),
            "mpg": float(settings.VEHICLE_MPG),
            "starting_fuel_gallons": round(starting_fuel_gallons, 2),
            "min_purchase_gallons": round(min_purchase_gallons, 2),
            "reserve_miles": round(reserve_miles, 2),
            "note": (
                "Cost counts only fuel purchased; the starting tank is "
                "assumed already paid for."
            ),
        },
        "timings_ms": {k: round(v, 1) for k, v in timings.items()},
    }
    cache.set(cache_key, payload, timeout=settings.ROUTE_CACHE_TTL)
    return payload
