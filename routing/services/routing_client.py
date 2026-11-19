"""OSRM routing client with Django cache (one call per unique OD pair)."""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache

from routing.services.external_calls import count_external_call

logger = logging.getLogger(__name__)


class RoutingError(Exception):
    """Unroutable / client routing failure (maps to HTTP 400)."""


class RoutingUpstreamError(Exception):
    """Upstream OSRM failure (maps to HTTP 502/503)."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class RouteResult:
    distance_miles: float
    duration_seconds: float
    geometry: dict[str, Any]  # GeoJSON LineString
    coordinates: list[list[float]]  # [lng, lat] pairs


def _cache_key(start_lat: float, start_lng: float, end_lat: float, end_lng: float) -> str:
    raw = f"{start_lat:.5f},{start_lng:.5f}:{end_lat:.5f},{end_lng:.5f}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"route:v3:{digest}"


def fetch_route(
    start_lat: float,
    start_lng: float,
    end_lat: float,
    end_lng: float,
) -> RouteResult:
    """
    Fetch a driving route from OSRM (overview=full, geometries=geojson).

    Cached by rounded coordinates so warm repeats make zero external calls.
    Retries once on transient network/5xx errors.
    """
    key = _cache_key(start_lat, start_lng, end_lat, end_lng)
    cached = cache.get(key)
    if cached:
        logger.info("route cache hit for %s", key)
        return RouteResult(
            distance_miles=cached["distance_miles"],
            duration_seconds=cached["duration_seconds"],
            geometry=cached["geometry"],
            coordinates=cached["coordinates"],
        )

    url = (
        f"{settings.OSRM_BASE_URL}/route/v1/driving/"
        f"{start_lng},{start_lat};{end_lng},{end_lat}"
    )
    params = {
        "overview": "full",
        "geometries": "geojson",
        "steps": "false",
    }
    timeout = float(settings.HTTP_TIMEOUT_SECONDS)
    payload: dict[str, Any] | None = None
    last_exc: Exception | None = None

    for attempt in range(2):
        try:
            count_external_call()
            response = requests.get(url, params=params, timeout=timeout)
            if response.status_code == 429:
                raise RoutingUpstreamError(
                    "Routing service rate-limited. Please retry shortly.",
                    status_code=503,
                )
            if response.status_code >= 500:
                raise RoutingUpstreamError(
                    f"Routing service unavailable (HTTP {response.status_code}).",
                    status_code=502,
                )
            if response.status_code >= 400:
                # OSRM often returns 200 with code != Ok; treat other 4xx as upstream.
                raise RoutingUpstreamError(
                    f"Routing service error (HTTP {response.status_code}).",
                    status_code=502,
                )
            payload = response.json()
            break
        except RoutingUpstreamError as exc:
            if attempt == 0 and exc.status_code == 502:
                time.sleep(0.35)
                last_exc = exc
                continue
            raise
        except requests.Timeout as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(0.35)
                continue
            raise RoutingUpstreamError(
                "Routing service timed out.", status_code=503
            ) from exc
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(0.35)
                continue
            raise RoutingUpstreamError(
                f"Routing service error: {exc}", status_code=502
            ) from exc

    if payload is None:
        raise RoutingUpstreamError(
            f"Routing service error: {last_exc}", status_code=502
        )

    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise RoutingError(
            payload.get("message")
            or "No driving route found between the given points (unroutable)."
        )

    route = payload["routes"][0]
    geometry = route["geometry"]
    if geometry.get("type") != "LineString" or not geometry.get("coordinates"):
        raise RoutingError("Routing service returned invalid geometry.")

    meters = float(route["distance"])
    result = RouteResult(
        distance_miles=meters / 1609.344,
        duration_seconds=float(route.get("duration") or 0.0),
        geometry=geometry,
        coordinates=geometry["coordinates"],
    )

    cache.set(
        key,
        {
            "distance_miles": result.distance_miles,
            "duration_seconds": result.duration_seconds,
            "geometry": result.geometry,
            "coordinates": result.coordinates,
        },
        timeout=settings.ROUTE_CACHE_TTL,
    )
    return result
