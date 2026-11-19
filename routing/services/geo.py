"""Pure geographic helpers (haversine, polyline distance). No Django imports."""

from __future__ import annotations

import math
from typing import Sequence

EARTH_RADIUS_MILES = 3958.7613


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in miles between two WGS84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(a)))


def cumulative_route_miles(
    coordinates: Sequence[Sequence[float]],
) -> list[float]:
    """
    Cumulative distance along a GeoJSON LineString coordinate list.

    ``coordinates`` is a sequence of ``[lng, lat]`` pairs (GeoJSON order).
    Returns a list of mile markers of the same length (first entry is 0.0).
    """
    if not coordinates:
        return []
    miles = [0.0]
    total = 0.0
    for i in range(1, len(coordinates)):
        lng1, lat1 = coordinates[i - 1]
        lng2, lat2 = coordinates[i]
        total += haversine_miles(lat1, lng1, lat2, lng2)
        miles.append(total)
    return miles


def simplify_route_coordinates(
    coordinates: Sequence[Sequence[float]],
    *,
    tolerance_miles: float = 0.25,
) -> list[list[float]]:
    """Reduce GeoJSON route vertices while keeping deviations within tolerance."""
    if len(coordinates) <= 2:
        return [list(point) for point in coordinates]
    if tolerance_miles < 0:
        raise ValueError("tolerance_miles must be non-negative")

    kept = [False] * len(coordinates)
    kept[0] = kept[-1] = True
    pending = [(0, len(coordinates) - 1)]
    while pending:
        first, last = pending.pop()
        max_distance = tolerance_miles
        split_at = None
        for i in range(first + 1, last):
            point = coordinates[i]
            start = coordinates[first]
            end = coordinates[last]
            distance, _ = point_to_segment_miles(
                point[1],
                point[0],
                start[1],
                start[0],
                end[1],
                end[0],
            )
            if distance > max_distance:
                max_distance = distance
                split_at = i
        if split_at is not None:
            kept[split_at] = True
            pending.append((first, split_at))
            pending.append((split_at, last))

    return [list(point) for i, point in enumerate(coordinates) if kept[i]]


def point_to_segment_miles(
    plat: float,
    plng: float,
    alat: float,
    alng: float,
    blat: float,
    blng: float,
) -> tuple[float, float]:
    """
    Approximate distance from point P to segment AB on a local tangent plane.

    Returns ``(distance_miles, t)`` where ``t`` in [0, 1] is the projection
    parameter along AB.
    """
    # Equirectangular local projection centered on A (good enough for ~10 mi).
    cos_lat = math.cos(math.radians(alat))
    ax, ay = 0.0, 0.0
    bx = (blng - alng) * cos_lat * EARTH_RADIUS_MILES * math.pi / 180.0
    by = (blat - alat) * EARTH_RADIUS_MILES * math.pi / 180.0
    px = (plng - alng) * cos_lat * EARTH_RADIUS_MILES * math.pi / 180.0
    py = (plat - alat) * EARTH_RADIUS_MILES * math.pi / 180.0

    ab2 = bx * bx + by * by
    if ab2 < 1e-12:
        return math.hypot(px, py), 0.0

    t = max(0.0, min(1.0, (px * bx + py * by) / ab2))
    cx, cy = ax + t * bx, ay + t * by
    return math.hypot(px - cx, py - cy), t


def nearest_mile_marker(
    lat: float,
    lng: float,
    coordinates: Sequence[Sequence[float]],
    cum_miles: Sequence[float],
) -> tuple[float, float]:
    """
    Find the nearest point on the route polyline to ``(lat, lng)``.

    Returns ``(mile_marker, distance_to_route_miles)``.
    """
    if not coordinates or not cum_miles:
        return 0.0, float("inf")

    best_dist = float("inf")
    best_mile = 0.0

    for i in range(len(coordinates) - 1):
        lng1, lat1 = coordinates[i]
        lng2, lat2 = coordinates[i + 1]
        dist, t = point_to_segment_miles(lat, lng, lat1, lng1, lat2, lng2)
        if dist < best_dist:
            best_dist = dist
            seg_len = cum_miles[i + 1] - cum_miles[i]
            best_mile = cum_miles[i] + t * seg_len

    # Also check last vertex.
    last_lng, last_lat = coordinates[-1]
    d_last = haversine_miles(lat, lng, last_lat, last_lng)
    if d_last < best_dist:
        return cum_miles[-1], d_last

    return best_mile, best_dist
