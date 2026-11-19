"""Unit tests for the pure dynamic-programming fuel optimizer."""

from __future__ import annotations

import pytest

from routing.services.optimizer import (
    CandidateStation,
    UnreachableRouteError,
    optimize_fuel_stops,
)


def _st(
    sid: int,
    mile: float,
    price: float,
    name: str = "S",
) -> CandidateStation:
    return CandidateStation(
        id=sid,
        name=f"{name}{sid}",
        city="Town",
        state="TX",
        lat=0.0,
        lng=0.0,
        price=price,
        mile_marker=mile,
    )


def test_short_route_zero_stops():
    result = optimize_fuel_stops(
        [_st(1, 100, 3.0)],
        total_distance_miles=400,
        max_range_miles=500,
        mpg=10,
    )
    assert result.stops == []
    assert result.total_gallons == 0
    assert result.total_cost_usd == 0


def test_multi_stop_long_route():
    stations = [
        _st(1, 200, 4.00),
        _st(2, 400, 3.00),
        _st(3, 600, 3.50),
        _st(4, 800, 2.50),
        _st(5, 1000, 3.00),
    ]
    result = optimize_fuel_stops(
        stations,
        total_distance_miles=1200,
        max_range_miles=500,
        mpg=10,
    )
    assert len(result.stops) >= 2
    assert result.total_gallons > 0
    assert result.total_cost_usd > 0
    for stop in result.stops:
        assert stop.gallons_purchased <= 50.0 + 1e-6


def test_no_station_gap_raises():
    stations = [
        _st(1, 100, 3.0),
        _st(2, 850, 3.0),
    ]
    with pytest.raises(UnreachableRouteError):
        optimize_fuel_stops(
            stations,
            total_distance_miles=900,
            max_range_miles=500,
            mpg=10,
        )


def test_cheaper_ahead_buys_just_enough():
    stations = [
        _st(1, 200, 4.00),
        _st(2, 400, 3.50),
        _st(3, 700, 2.00),
    ]
    result = optimize_fuel_stops(
        stations,
        total_distance_miles=1000,
        max_range_miles=500,
        mpg=10,
    )
    assert result.stops[0].station.id == 2
    assert result.stops[0].gallons_purchased == pytest.approx(22.5, abs=0.01)
    assert result.stops[0].cost_usd == pytest.approx(78.75, abs=0.01)
    assert result.stops[1].station.id == 3
    assert result.stops[1].gallons_purchased == pytest.approx(30.0, abs=0.01)


def test_price_tie_prefers_earlier_mile():
    stations = [
        _st(1, 100, 3.00),
        _st(2, 150, 3.00),
        _st(3, 400, 3.00),
    ]
    result = optimize_fuel_stops(
        stations,
        total_distance_miles=700,
        max_range_miles=500,
        mpg=10,
    )
    assert result.stops[0].station.id == 1


def test_price_tie_at_same_mile_prefers_lower_station_id():
    stations = [
        _st(2, 100, 3.00),
        _st(1, 100, 3.00),
        _st(3, 400, 3.00),
    ]
    result = optimize_fuel_stops(
        stations,
        total_distance_miles=700,
        max_range_miles=500,
        mpg=10,
    )
    assert result.stops[0].station.id == 1


def test_starting_fuel_partial_requires_early_stop():
    """With 10 gallons and a 25-mile reserve, first stop is within 75 miles."""
    stations = [
        _st(1, 70, 3.50),
        _st(2, 250, 2.50),
        _st(3, 450, 3.00),
    ]
    result = optimize_fuel_stops(
        stations,
        total_distance_miles=600,
        max_range_miles=500,
        mpg=10,
        starting_fuel_gallons=10.0,
    )
    assert result.stops
    assert result.stops[0].station.mile_marker <= 75.0 + 1e-6
    assert result.stops[0].station.id == 1


def test_starting_fuel_unreachable_raises():
    """No station within starting range after preserving the reserve."""
    stations = [
        _st(1, 200, 3.0),  # beyond 100 mi reach from 10 gal
    ]
    with pytest.raises(UnreachableRouteError):
        optimize_fuel_stops(
            stations,
            total_distance_miles=400,
            max_range_miles=500,
            mpg=10,
            starting_fuel_gallons=10.0,
        )


def test_subminimum_purchase_is_allowed_when_no_station_is_reachable():
    stations = [
        _st(1, 400, 5.00),
        _st(2, 600, 4.00),
        _st(3, 620, 3.00),
        _st(4, 1080, 3.50),
    ]
    result = optimize_fuel_stops(
        stations,
        total_distance_miles=1300,
        max_range_miles=500,
        mpg=10,
        min_purchase_gallons=5,
        reserve_miles=25,
    )
    stop_b = next(stop for stop in result.stops if stop.station.id == 2)
    assert stop_b.gallons_purchased == pytest.approx(2.0)


def test_final_purchase_can_be_below_minimum():
    result = optimize_fuel_stops(
        [_st(1, 450, 3.00)],
        total_distance_miles=500,
        max_range_miles=500,
        mpg=10,
        min_purchase_gallons=5,
        reserve_miles=25,
    )
    assert len(result.stops) == 1
    assert result.stops[0].gallons_purchased == pytest.approx(2.5)


def test_below_minimum_allowed_when_needed_to_reach_next_station():
    stations = [
        _st(1, 400, 4.00),
        _st(2, 430, 5.00),
        _st(3, 880, 3.00),
    ]
    result = optimize_fuel_stops(
        stations,
        total_distance_miles=1200,
        max_range_miles=500,
        mpg=10,
        min_purchase_gallons=5,
        reserve_miles=25,
    )
    emergency_stop = next(stop for stop in result.stops if stop.station.id == 2)
    assert emergency_stop.gallons_purchased == pytest.approx(0.5)


def test_reserve_limits_each_leg_to_effective_range():
    stations = [_st(1, 480, 3.00)]
    with pytest.raises(UnreachableRouteError):
        optimize_fuel_stops(
            stations,
            total_distance_miles=600,
            max_range_miles=500,
            mpg=10,
            reserve_miles=25,
        )


def test_all_legs_preserve_reserve_with_minimum_purchase():
    stations = [
        _st(1, 400, 5.00),
        _st(2, 600, 4.00),
        _st(3, 620, 3.00),
        _st(4, 1080, 3.50),
    ]
    result = optimize_fuel_stops(
        stations,
        total_distance_miles=1300,
        max_range_miles=500,
        mpg=10,
        min_purchase_gallons=5,
        reserve_miles=25,
    )
    mile_markers = [0.0] + [s.station.mile_marker for s in result.stops] + [1300.0]
    assert max(b - a for a, b in zip(mile_markers, mile_markers[1:])) <= 475.0
