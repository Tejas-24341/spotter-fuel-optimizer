"""Randomized comparison against an independent discrete fuel-planning oracle."""

from __future__ import annotations

import random

import pytest

from routing.services.optimizer import (
    CandidateStation,
    UnreachableRouteError,
    optimize_fuel_stops,
)


def _oracle_cost(
    stations: list[tuple[int, float]],
    distance: int,
    *,
    max_range: int,
    mpg: int,
    starting_fuel: float,
    reserve: int,
    min_purchase: float,
) -> float:
    """Find the cheapest plan using a coarser 10-mile fuel-state grid."""
    unit_miles = 10
    capacity = round((max_range - reserve) / unit_miles)
    initial_fuel = round(
        (starting_fuel - reserve / mpg) * mpg / unit_miles
    )
    min_units = round(min_purchase * mpg / unit_miles)
    ordered = sorted((round(mile / unit_miles), price) for mile, price in stations)
    destination = round(distance / unit_miles)

    # Each layer maps fuel remaining on arrival to the cheapest cost so far.
    best: float | None = None
    # Use an explicit station-index/fuel-state table to permit skipping stations.
    arrivals: list[dict[int, float]] = [dict() for _ in ordered]
    for i, (mile, _) in enumerate(ordered):
        gap = mile
        if gap <= initial_fuel:
            arrivals[i][initial_fuel - gap] = 0.0

    for i, (mile, price) in enumerate(ordered):
        for fuel, cost in tuple(arrivals[i].items()):
            to_destination = destination - mile
            if to_destination <= fuel:
                best = cost if best is None else min(best, cost)
            elif to_destination <= capacity:
                terminal_purchase = to_destination - fuel
                terminal_cost = cost + terminal_purchase * unit_miles / mpg * price
                best = terminal_cost if best is None else min(best, terminal_cost)

            capacity_left = capacity - fuel
            next_stations = [
                (j, next_mile - mile)
                for j, (next_mile, _) in enumerate(ordered[i + 1 :], start=i + 1)
            ]
            can_reach_without_purchase = any(gap <= fuel for _, gap in next_stations)
            destination_reachable = destination - mile <= capacity
            emergency = not can_reach_without_purchase

            for purchase in range(capacity_left + 1):
                if (
                    0 < purchase < min_units
                    and not destination_reachable
                    and not emergency
                ):
                    continue
                if purchase == 0:
                    purchase_cost = 0.0
                else:
                    purchase_cost = purchase * unit_miles / mpg * price
                after_purchase = fuel + purchase
                for j, gap in next_stations:
                    if gap <= after_purchase:
                        remaining = after_purchase - gap
                        new_cost = cost + purchase_cost
                        old_cost = arrivals[j].get(remaining)
                        if old_cost is None or new_cost < old_cost:
                            arrivals[j][remaining] = new_cost

    if best is None:
        raise UnreachableRouteError("oracle route is unreachable")
    return best


def _station(sid: int, mile: int, price: float) -> CandidateStation:
    return CandidateStation(
        id=sid,
        name=f"Station {sid}",
        city="Town",
        state="TX",
        lat=0.0,
        lng=0.0,
        price=price,
        mile_marker=float(mile),
    )


def _assert_plan_invariants(
    result,
    distance: int,
    *,
    max_range: int,
    mpg: int,
    starting_fuel: float,
    reserve: int,
    min_purchase: float,
    station_miles: list[int],
) -> None:
    usable_range = max_range - reserve
    positions = [0.0] + [stop.station.mile_marker for stop in result.stops] + [
        float(distance)
    ]
    assert all(b - a <= usable_range + 0.01 for a, b in zip(positions, positions[1:]))

    fuel = starting_fuel * mpg
    last_position = 0.0
    for stop in result.stops:
        distance_to_stop = stop.station.mile_marker - last_position
        fuel -= distance_to_stop
        assert fuel >= reserve - 0.01
        purchase = stop.gallons_purchased
        destination_reachable = distance - stop.station.mile_marker <= usable_range
        if 0 < purchase < min_purchase - 0.01:
            next_station_reachable = any(
                stop.station.mile_marker < station_mile
                <= stop.station.mile_marker + fuel - reserve * mpg
                for station_mile in station_miles
            )
            assert destination_reachable or not next_station_reachable
        fuel += purchase * mpg
        assert fuel <= max_range + 0.01
        last_position = stop.station.mile_marker

    fuel -= distance - last_position
    assert fuel >= reserve - 0.01
    assert sum(stop.gallons_purchased for stop in result.stops) == pytest.approx(
        result.total_gallons, abs=0.01
    )
    assert (
        starting_fuel
        + result.total_gallons
        - distance / mpg
        == pytest.approx(fuel / mpg, abs=0.02)
    )


def test_randomized_optimizer_matches_independent_oracle():
    rng = random.Random(731_904)
    max_range = 500
    mpg = 10
    cases = 220

    for case in range(cases):
        distance = rng.randint(90, 220) * 10
        reserve = rng.choice([0, 10, 20, 30, 40])
        capacity = max_range - reserve
        min_purchase = rng.choice([1.0, 2.0, 3.0, 4.0, 5.0])
        min_purchase = min(min_purchase, capacity / mpg)
        starting_fuel = rng.randint(max(10, reserve // mpg + 5), max_range // mpg)
        starting_reach = round((starting_fuel - reserve / mpg) * mpg)

        first_mile = rng.randint(2, min(14, starting_reach // 10)) * 10
        station_miles = [first_mile]
        while station_miles[-1] < distance - 300:
            station_miles.append(
                min(distance - 100, station_miles[-1] + rng.randint(8, 23) * 10)
            )
        station_miles = sorted(set(station_miles))
        stations = [
            (mile, rng.randint(200, 500) / 100)
            for mile in station_miles
        ]
        candidates = [
            _station(index + 1, mile, price)
            for index, (mile, price) in enumerate(stations)
        ]

        try:
            oracle_cost = _oracle_cost(
                stations,
                distance,
                max_range=max_range,
                mpg=mpg,
                starting_fuel=starting_fuel,
                reserve=reserve,
                min_purchase=min_purchase,
            )
        except UnreachableRouteError:
            with pytest.raises(UnreachableRouteError):
                optimize_fuel_stops(
                    candidates,
                    distance,
                    max_range_miles=max_range,
                    mpg=mpg,
                    starting_fuel_gallons=starting_fuel,
                    min_purchase_gallons=min_purchase,
                    reserve_miles=reserve,
                )
            continue

        result = optimize_fuel_stops(
            candidates,
            distance,
            max_range_miles=max_range,
            mpg=mpg,
            starting_fuel_gallons=starting_fuel,
            min_purchase_gallons=min_purchase,
            reserve_miles=reserve,
        )
        assert result.total_cost_usd == pytest.approx(
            oracle_cost, abs=0.01
        ), (
            f"case {case}: stations={stations}, distance={distance}, "
            f"starting_fuel={starting_fuel}, reserve={reserve}, "
            f"min_purchase={min_purchase}, stops="
            f"{[(stop.station.mile_marker, stop.station.price, stop.gallons_purchased) for stop in result.stops]}"
        )
        _assert_plan_invariants(
            result,
            distance,
            max_range=max_range,
            mpg=mpg,
            starting_fuel=starting_fuel,
            reserve=reserve,
            min_purchase=min_purchase,
            station_miles=station_miles,
        )
