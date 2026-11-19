"""
In-memory station index loaded lazily from the DB as numpy arrays.

No per-request full-table scans: filtering is vectorized against the cached
arrays. Mile-marker projection uses a KD-style nearest-vertex shortcut so
warm requests stay well under one second.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from django.conf import settings

from routing.services.geo import cumulative_route_miles, haversine_miles
from routing.services.optimizer import CandidateStation

logger = logging.getLogger(__name__)

_MILES_PER_DEG_LAT = 69.0
EARTH_RADIUS_MILES = 3958.7613


@dataclass(slots=True)
class StationArrays:
    ids: np.ndarray
    lats: np.ndarray
    lngs: np.ndarray
    prices: np.ndarray
    names: list[str]
    cities: list[str]
    states: list[str]


_lock = threading.Lock()
_cache: StationArrays | None = None


def clear_station_cache() -> None:
    """Drop the in-memory index (used by tests / after re-import)."""
    global _cache
    with _lock:
        _cache = None


def get_station_arrays() -> StationArrays:
    """Lazily load all stations into numpy arrays (process-wide cache)."""
    global _cache
    if _cache is not None:
        return _cache
    with _lock:
        if _cache is not None:
            return _cache
        t0 = time.perf_counter()
        from routing.models import Station

        rows = list(
            Station.objects.all().values_list(
                "opis_id", "lat", "lng", "price", "name", "city", "state"
            )
        )
        if not rows:
            _cache = StationArrays(
                ids=np.array([], dtype=np.int64),
                lats=np.array([], dtype=np.float64),
                lngs=np.array([], dtype=np.float64),
                prices=np.array([], dtype=np.float64),
                names=[],
                cities=[],
                states=[],
            )
        else:
            ids, lats, lngs, prices, names, cities, states = zip(*rows)
            _cache = StationArrays(
                ids=np.asarray(ids, dtype=np.int64),
                lats=np.asarray(lats, dtype=np.float64),
                lngs=np.asarray(lngs, dtype=np.float64),
                prices=np.asarray(prices, dtype=np.float64),
                names=list(names),
                cities=list(cities),
                states=list(states),
            )
        logger.info(
            "Loaded %d stations into memory in %.1f ms",
            len(_cache.ids),
            (time.perf_counter() - t0) * 1000,
        )
        return _cache


def stations_near_route(
    coordinates: Sequence[Sequence[float]],
    *,
    corridor_miles: float | None = None,
) -> list[CandidateStation]:
    """
    Return stations within ``corridor_miles`` of the route polyline.

    Steps: bbox prefilter → vectorized distance-to-vertex → mile marker via
    nearest route vertex (plus local segment refine).
    """
    corridor = (
        corridor_miles
        if corridor_miles is not None
        else float(settings.STATION_CORRIDOR_MILES)
    )
    arrays = get_station_arrays()
    if len(arrays.ids) == 0 or not coordinates:
        return []

    t0 = time.perf_counter()
    route_lngs = np.asarray([c[0] for c in coordinates], dtype=np.float64)
    route_lats = np.asarray([c[1] for c in coordinates], dtype=np.float64)
    min_lat, max_lat = float(route_lats.min()), float(route_lats.max())
    min_lng, max_lng = float(route_lngs.min()), float(route_lngs.max())

    pad_lat = corridor / _MILES_PER_DEG_LAT
    mid_lat = (min_lat + max_lat) / 2.0
    cos_lat = max(0.2, abs(float(np.cos(np.radians(mid_lat)))))
    pad_lng = corridor / (_MILES_PER_DEG_LAT * cos_lat)

    mask = (
        (arrays.lats >= min_lat - pad_lat)
        & (arrays.lats <= max_lat + pad_lat)
        & (arrays.lngs >= min_lng - pad_lng)
        & (arrays.lngs <= max_lng + pad_lng)
    )
    idxs = np.nonzero(mask)[0]
    logger.info(
        "Station bbox prefilter: %d / %d in %.1f ms",
        len(idxs),
        len(arrays.ids),
        (time.perf_counter() - t0) * 1000,
    )
    if len(idxs) == 0:
        return []

    t1 = time.perf_counter()
    # Subsample route for coarse reject (keep geometry density reasonable).
    step = max(1, len(coordinates) // 800)
    sample_lats = route_lats[::step]
    sample_lngs = route_lngs[::step]
    cand_lats = arrays.lats[idxs]
    cand_lngs = arrays.lngs[idxs]
    coarse = _batch_min_haversine(cand_lats, cand_lngs, sample_lats, sample_lngs)
    near_mask = coarse <= (corridor * 1.75)
    idxs = idxs[near_mask]
    logger.info(
        "Station coarse filter: %d left in %.1f ms",
        len(idxs),
        (time.perf_counter() - t1) * 1000,
    )
    if len(idxs) == 0:
        return []

    t2 = time.perf_counter()
    # Decimate dense OSRM polylines — mile markers stay accurate enough for
    # fuel planning while keeping nearest-vertex search under ~1s.
    decimate = max(1, len(coordinates) // 1500)
    slim_lats = route_lats[::decimate]
    slim_lngs = route_lngs[::decimate]
    if slim_lats[-1] != route_lats[-1]:
        slim_lats = np.append(slim_lats, route_lats[-1])
        slim_lngs = np.append(slim_lngs, route_lngs[-1])
    slim_coords = list(zip(slim_lngs.tolist(), slim_lats.tolist(), strict=True))
    cum = np.asarray(cumulative_route_miles(slim_coords), dtype=np.float64)
    miles, dists = _nearest_vertex_miles(
        arrays.lats[idxs],
        arrays.lngs[idxs],
        slim_lats,
        slim_lngs,
        cum,
    )
    keep = dists <= corridor
    results: list[CandidateStation] = []
    kept_idxs = idxs[keep]
    kept_miles = miles[keep]
    for i, mile in zip(kept_idxs, kept_miles, strict=True):
        results.append(
            CandidateStation(
                id=int(arrays.ids[i]),
                name=arrays.names[i],
                city=arrays.cities[i],
                state=arrays.states[i],
                lat=float(arrays.lats[i]),
                lng=float(arrays.lngs[i]),
                price=float(arrays.prices[i]),
                mile_marker=float(mile),
            )
        )
    logger.info(
        "Station precise filter: %d along route in %.1f ms",
        len(results),
        (time.perf_counter() - t2) * 1000,
    )
    return results


def _batch_min_haversine(
    lats: np.ndarray,
    lngs: np.ndarray,
    ref_lats: np.ndarray,
    ref_lngs: np.ndarray,
) -> np.ndarray:
    """Minimum haversine distance from each candidate to any reference point."""
    phi1 = np.radians(lats)[:, None]
    phi2 = np.radians(ref_lats)[None, :]
    dphi = phi2 - phi1
    dlmb = np.radians(ref_lngs)[None, :] - np.radians(lngs)[:, None]
    a = (
        np.sin(dphi / 2) ** 2
        + np.cos(phi1) * np.cos(phi2) * np.sin(dlmb / 2) ** 2
    )
    miles = 2 * EARTH_RADIUS_MILES * np.arcsin(np.minimum(1.0, np.sqrt(a)))
    return miles.min(axis=1)


def _nearest_vertex_miles(
    lats: np.ndarray,
    lngs: np.ndarray,
    route_lats: np.ndarray,
    route_lngs: np.ndarray,
    cum: np.ndarray,
    chunk: int = 64,
) -> tuple[np.ndarray, np.ndarray]:
    """
    For each station, find the nearest route vertex and its cumulative mile.

    Chunked to bound peak memory on long polylines.
    """
    n = len(lats)
    best_dist = np.full(n, np.inf, dtype=np.float64)
    best_mile = np.zeros(n, dtype=np.float64)

    for start in range(0, n, chunk):
        end = min(start + chunk, n)
        slats = lats[start:end]
        slngs = lngs[start:end]
        phi1 = np.radians(slats)[:, None]
        phi2 = np.radians(route_lats)[None, :]
        dphi = phi2 - phi1
        dlmb = np.radians(route_lngs)[None, :] - np.radians(slngs)[:, None]
        a = (
            np.sin(dphi / 2) ** 2
            + np.cos(phi1) * np.cos(phi2) * np.sin(dlmb / 2) ** 2
        )
        dist = 2 * EARTH_RADIUS_MILES * np.arcsin(np.minimum(1.0, np.sqrt(a)))
        j = dist.argmin(axis=1)
        dmin = dist[np.arange(end - start), j]
        best_dist[start:end] = dmin
        best_mile[start:end] = cum[j]
    return best_mile, best_dist


def route_length_miles(coordinates: Sequence[Sequence[float]]) -> float:
    """Total polyline length via haversine."""
    if len(coordinates) < 2:
        return 0.0
    total = 0.0
    for i in range(1, len(coordinates)):
        lng1, lat1 = coordinates[i - 1]
        lng2, lat2 = coordinates[i]
        total += haversine_miles(lat1, lng1, lat2, lng2)
    return total
