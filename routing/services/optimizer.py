"""
Cost-optimal fuel-stop planner.

Pure functions only — no Django / network / I/O imports.

Assumptions
-----------
* Vehicle starts with a configurable tank level (default: **full** = 50 gal).
* Fuel purchases are optimized across the complete sequence of route stations.
* Cost counts only fuel purchased; the starting tank is treated as already paid.
* If the whole route fits in the starting fuel, zero stops and zero cost.
* Price ties: prefer the earlier mile marker, then lower station id.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

import numpy as np

@dataclass(frozen=True, slots=True)
class CandidateStation:
    """A station projected onto the route with a mile marker."""

    id: int
    name: str
    city: str
    state: str
    lat: float
    lng: float
    price: float
    mile_marker: float


@dataclass(frozen=True, slots=True)
class FuelStopPlan:
    station: CandidateStation
    gallons_purchased: float
    cost_usd: float


@dataclass(frozen=True, slots=True)
class OptimizeResult:
    stops: list[FuelStopPlan]
    total_gallons: float
    total_cost_usd: float


class UnreachableRouteError(Exception):
    """Raised when a gap exceeds vehicle range with no reachable station."""


def optimize_fuel_stops(
    stations: Sequence[CandidateStation],
    total_distance_miles: float,
    *,
    max_range_miles: float = 500.0,
    mpg: float = 10.0,
    starting_fuel_gallons: float | None = None,
    min_purchase_gallons: float = 5.0,
    reserve_miles: float = 25.0,
) -> OptimizeResult:
    """
    Dynamic-programming gas-station planner along a 1-D route.

    Fuel stops preserve ``reserve_miles`` in the tank, so refueling capacity
    provides ``max_range_miles - reserve_miles`` miles of travel. Purchases
    below ``min_purchase_gallons`` are skipped when the current fuel reaches
    another station; otherwise the minimum needed to reach a station is allowed.
    Purchases when the destination is within usable range are exempt.
    """
    if total_distance_miles < 0:
        raise ValueError("total_distance_miles must be non-negative")
    if max_range_miles <= 0 or mpg <= 0:
        raise ValueError("max_range_miles and mpg must be positive")
    if reserve_miles < 0 or reserve_miles >= max_range_miles:
        raise ValueError("reserve_miles must be non-negative and less than max_range_miles")
    if min_purchase_gallons < 0:
        raise ValueError("min_purchase_gallons must be non-negative")

    full_tank_gallons = max_range_miles / mpg
    reserve_gallons = reserve_miles / mpg
    effective_range_miles = max_range_miles - reserve_miles
    effective_capacity_gallons = full_tank_gallons - reserve_gallons
    if min_purchase_gallons > effective_capacity_gallons + 1e-9:
        raise ValueError(
            "min_purchase_gallons must not exceed tank capacity after reserve"
        )
    if starting_fuel_gallons is None:
        starting_fuel_gallons = full_tank_gallons
    if starting_fuel_gallons < 0 or starting_fuel_gallons > full_tank_gallons + 1e-9:
        raise ValueError(
            f"starting_fuel_gallons must be between 0 and {full_tank_gallons:.0f}"
        )

    if starting_fuel_gallons + 1e-9 < reserve_gallons:
        raise UnreachableRouteError(
            f"Starting fuel must include at least {reserve_gallons:.2f} gallons "
            f"to preserve the {reserve_miles:.0f}-mile reserve."
        )
    fuel_miles = (starting_fuel_gallons - reserve_gallons) * mpg

    # Entire trip covered while preserving the reserve.
    if total_distance_miles <= fuel_miles + 1e-9:
        return OptimizeResult(stops=[], total_gallons=0.0, total_cost_usd=0.0)

    usable = sorted(
        (s for s in stations if 0.0 < s.mile_marker < total_distance_miles),
        key=lambda s: (s.mile_marker, s.price, s.id),
    )
    if not usable:
        raise UnreachableRouteError(
            "No fuel station is available before the starting fuel runs out."
        )

    whole_mile_inputs = (
        abs(total_distance_miles - round(total_distance_miles)) <= 1e-9
        and abs(fuel_miles - round(fuel_miles)) <= 1e-9
        and abs(effective_range_miles - round(effective_range_miles)) <= 1e-9
        and abs(min_purchase_gallons * mpg - round(min_purchase_gallons * mpg))
        <= 1e-9
        and all(abs(station.mile_marker - round(station.mile_marker)) <= 1e-9 for station in usable)
    )
    scale = 1 if whole_mile_inputs else 100
    capacity_units = math.floor(effective_range_miles * scale + 1e-9)
    starting_units = math.floor(fuel_miles * scale + 1e-9)
    minimum_units = math.ceil(min_purchase_gallons * mpg * scale - 1e-9)
    unit_cost_factor = 1.0 / (scale * mpg)
    station_count = len(usable)
    state_count = capacity_units + 1
    inf = float("inf")

    arrivals = [np.full(state_count, inf, dtype=np.float64) for _ in usable]
    parent_stations = [
        np.full(state_count, -2, dtype=np.int32) for _ in usable
    ]
    parent_fuels = [
        np.full(state_count, -1, dtype=np.int32) for _ in usable
    ]
    first_stops = [
        np.full(state_count, station_count, dtype=np.int32)
        for _ in usable
    ]

    def ceil_units(miles: float) -> int:
        return math.ceil(miles * scale - 1e-9)

    for i, station in enumerate(usable):
        gap_units = ceil_units(station.mile_marker)
        if gap_units <= starting_units:
            fuel_units = starting_units - gap_units
            arrivals[i][fuel_units] = 0.0
            parent_stations[i][fuel_units] = -1

    best_cost = inf
    best_station = -1
    best_fuel = -1
    best_terminal_purchase = 0
    best_first_stop = station_count

    for i, station in enumerate(usable):
        arrival_cost = arrivals[i]
        if not np.isfinite(arrival_cost).any():
            continue

        fuel_values = np.arange(state_count, dtype=np.float64)
        weighted_costs = arrival_cost - fuel_values * (
            station.price * unit_cost_factor
        )
        prefix_costs = np.minimum.accumulate(weighted_costs)
        prefix_is_best = np.isfinite(weighted_costs) & (
            weighted_costs <= prefix_costs + 1e-12
        )
        prefix_first_stops = np.minimum.accumulate(
            np.where(prefix_is_best, first_stops[i], station_count)
        )
        fuel_indices = np.arange(state_count, dtype=np.int32)
        prefix_sources = np.maximum.accumulate(
            np.where(
                prefix_is_best & (first_stops[i] == prefix_first_stops),
                fuel_indices,
                -1,
            )
        )

        post_cost = arrival_cost.copy()
        post_source = np.where(
            np.isfinite(arrival_cost), fuel_indices, -1
        ).astype(np.int32)
        post_first_stop = first_stops[i].copy()
        destination_reachable = (
            total_distance_miles - station.mile_marker
            <= effective_range_miles + 1e-9
        )
        nearest_gap = (
            ceil_units(usable[i + 1].mile_marker - station.mile_marker)
            if i + 1 < station_count
            else state_count
        )

        post_fuels = fuel_indices
        post_unit_cost = station.price * unit_cost_factor

        def apply_purchase_candidates(limits: np.ndarray) -> None:
            clipped_limits = np.clip(limits, 0, state_count - 1)
            sources = prefix_sources[clipped_limits]
            valid = (limits >= 0) & (sources >= 0)
            if not np.any(valid):
                return
            candidate_costs = (
                prefix_costs[clipped_limits] + post_fuels * post_unit_cost
            )
            source_first = first_stops[i][np.maximum(sources, 0)]
            candidate_first = np.where(
                (post_fuels > sources) & (source_first == station_count),
                i,
                source_first,
            )
            finite_pair = np.isfinite(candidate_costs) & np.isfinite(post_cost)
            cost_difference = np.zeros_like(candidate_costs)
            np.subtract(
                candidate_costs,
                post_cost,
                out=cost_difference,
                where=finite_pair,
            )
            equal_cost = finite_pair & (np.abs(cost_difference) <= 1e-12)
            better = valid & (
                (candidate_costs < post_cost - 1e-12)
                | (equal_cost & (candidate_first < post_first_stop))
            )
            post_cost[better] = candidate_costs[better]
            post_source[better] = sources[better]
            post_first_stop[better] = candidate_first[better]

        apply_purchase_candidates(post_fuels - minimum_units)
        exceptional_limits = post_fuels - 1
        if not destination_reachable:
            exceptional_limits = np.minimum(
                exceptional_limits, nearest_gap - 1
            )
        apply_purchase_candidates(exceptional_limits)

        for j in range(i + 1, station_count):
            gap_units = ceil_units(
                usable[j].mile_marker - station.mile_marker
            )
            if gap_units > capacity_units:
                break
            available = post_cost[gap_units:]
            target = arrivals[j][: state_count - gap_units]
            improve = available < target - 1e-12
            if not np.any(improve):
                continue
            target[improve] = available[improve]
            parent_stations[j][: state_count - gap_units][improve] = i
            parent_fuels[j][: state_count - gap_units][improve] = (
                post_source[gap_units:][improve]
            )
            first_stops[j][: state_count - gap_units][improve] = (
                post_first_stop[gap_units:][improve]
            )

        destination_gap = total_distance_miles - station.mile_marker
        if destination_gap <= effective_range_miles + 1e-9:
            destination_units = ceil_units(destination_gap)
            if destination_units <= capacity_units:
                reachable_fuels = np.flatnonzero(np.isfinite(arrival_cost))
                purchase_units = np.maximum(0, destination_units - reachable_fuels)
                candidate_costs = (
                    arrival_cost[reachable_fuels]
                    + purchase_units * station.price * unit_cost_factor
                )
                candidate_first_stops = first_stops[i][reachable_fuels].copy()
                candidate_first_stops[
                    (purchase_units > 0)
                    & (candidate_first_stops == station_count)
                ] = i
                minimum_cost = float(candidate_costs.min())
                tied = np.flatnonzero(candidate_costs <= minimum_cost + 1e-12)
                chosen = tied[
                    np.argmin(candidate_first_stops[tied])
                ]
                first_stop = int(candidate_first_stops[chosen])
                candidate = float(candidate_costs[chosen])
                if candidate < best_cost - 1e-12 or (
                    abs(candidate - best_cost) <= 1e-12
                    and first_stop < best_first_stop
                ):
                    best_cost = candidate
                    best_station = i
                    best_fuel = int(reachable_fuels[chosen])
                    best_terminal_purchase = int(purchase_units[chosen])
                    best_first_stop = first_stop

    if best_station < 0:
        raise UnreachableRouteError(
            "No sequence of fuel stops can reach the destination while "
            f"preserving the {reserve_miles:.0f}-mile reserve."
        )

    reversed_purchases: list[tuple[int, int]] = []
    if best_terminal_purchase:
        reversed_purchases.append((best_station, best_terminal_purchase))
    current_station = best_station
    current_fuel = best_fuel
    while True:
        previous_station = int(parent_stations[current_station][current_fuel])
        if previous_station < 0:
            break
        previous_fuel = int(parent_fuels[current_station][current_fuel])
        leg_units = ceil_units(
            usable[current_station].mile_marker
            - usable[previous_station].mile_marker
        )
        purchase_units = current_fuel + leg_units - previous_fuel
        if purchase_units > 0:
            reversed_purchases.append((previous_station, purchase_units))
        current_station = previous_station
        current_fuel = previous_fuel

    purchases = list(reversed(reversed_purchases))
    plans: list[FuelStopPlan] = []
    actual_fuel_miles = starting_fuel_gallons * mpg
    previous_position = 0.0
    for station_index, purchase_units in purchases:
        station = usable[station_index]
        raw_gallons = purchase_units * unit_cost_factor
        gallons = math.ceil(raw_gallons * 100 - 1e-9) / 100
        actual_fuel_miles -= station.mile_marker - previous_position
        available_gallons = max(
            0.0, (max_range_miles - actual_fuel_miles) / mpg
        )
        gallons = min(
            gallons,
            math.floor(available_gallons * 100 + 1e-9) / 100,
        )
        if gallons <= 0:
            previous_position = station.mile_marker
            continue
        plans.append(
            FuelStopPlan(
                station=station,
                gallons_purchased=gallons,
                cost_usd=round(gallons * station.price, 2),
            )
        )
        actual_fuel_miles += gallons * mpg
        previous_position = station.mile_marker

    return OptimizeResult(
        stops=plans,
        total_gallons=round(sum(plan.gallons_purchased for plan in plans), 2),
        total_cost_usd=round(sum(plan.cost_usd for plan in plans), 2),
    )
