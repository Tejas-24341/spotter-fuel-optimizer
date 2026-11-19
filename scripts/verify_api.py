"""Verify live route plans, caching, and debug external-call counts."""

from __future__ import annotations

import argparse
import time

import requests


ROUTES = [
    ("New York, NY", "Los Angeles, CA"),
    ("Dallas, TX", "Houston, TX"),
    ("Seattle, WA", "Miami, FL"),
    ("Chicago, IL", "Denver, CO"),
]


def _validate(payload: dict) -> tuple[float, int, float]:
    distance = float(payload["total_distance_miles"])
    stops = payload["fuel_stops"]
    assumptions = payload["assumptions"]
    mpg = float(assumptions["mpg"])
    reserve = float(assumptions["reserve_miles"])
    max_range = float(assumptions["max_range_miles"])
    effective_range = max_range - reserve
    starting_fuel = float(assumptions["starting_fuel_gallons"])
    total_gallons = float(payload["total_gallons"])

    positions = [0.0] + [float(stop["mile_marker"]) for stop in stops] + [distance]
    gaps = [b - a for a, b in zip(positions, positions[1:])]
    largest_gap = max(gaps, default=distance)
    assert all(gap <= effective_range + 0.02 for gap in gaps), (
        f"route gap {largest_gap:.2f} exceeds usable range {effective_range:.2f}"
    )

    fuel = starting_fuel * mpg
    previous_position = 0.0
    for stop in stops:
        position = float(stop["mile_marker"])
        fuel -= position - previous_position
        assert fuel >= reserve - 0.1, "fuel fell below reserve before a stop"
        fuel += float(stop["gallons_purchased"]) * mpg
        assert fuel <= max_range + 0.1, "purchase exceeded tank capacity"
        previous_position = position
    fuel -= distance - previous_position
    assert fuel >= reserve - 0.1, "fuel fell below reserve at destination"

    stop_gallons = sum(float(stop["gallons_purchased"]) for stop in stops)
    assert abs(total_gallons - stop_gallons) <= 0.02
    assert abs(
        starting_fuel + total_gallons - distance / mpg - fuel / mpg
    ) <= 0.03, "total gallons do not balance distance / mpg and ending fuel"
    stop_cost = sum(float(stop["cost_usd"]) for stop in stops)
    assert abs(float(payload["total_fuel_cost_usd"]) - stop_cost) <= 0.02
    return distance, len(stops), largest_gap


def _request(
    session: requests.Session, url: str, start: str, finish: str
) -> tuple[dict, float, int]:
    before = time.perf_counter()
    response = session.post(
        url,
        json={"start": start, "finish": finish},
        timeout=120,
    )
    elapsed_ms = (time.perf_counter() - before) * 1000
    try:
        payload = response.json()
    except ValueError as exc:
        raise AssertionError(
            f"{start} to {finish}: response was not JSON "
            f"(HTTP {response.status_code})"
        ) from exc
    assert response.status_code == 200, (
        f"{start} to {finish}: HTTP {response.status_code}: {payload}"
    )
    external_calls = int(response.headers.get("X-External-Calls", "0"))
    return payload, elapsed_ms, external_calls


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    url = f"{args.base_url.rstrip('/')}/api/route/"

    print(
        "| Route | Distance (mi) | Stops | Cost ($) | Largest gap (mi) | "
        "Cold (ms) | Cached (ms) | Cold calls | Cached calls |"
    )
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    with requests.Session() as session:
        for start, finish in ROUTES:
            cold_payload, cold_ms, external_calls = _request(
                session, url, start, finish
            )
            distance, stop_count, largest_gap = _validate(cold_payload)
            cached_payload, cached_ms, cached_calls = _request(
                session, url, start, finish
            )
            _validate(cached_payload)
            assert cached_payload["total_distance_miles"] == cold_payload[
                "total_distance_miles"
            ]
            assert cached_payload["total_fuel_cost_usd"] == cold_payload[
                "total_fuel_cost_usd"
            ]
            assert cached_calls == 0, (
                f"cached {start} to {finish} request made "
                f"{cached_calls} external calls"
            )
            assert cached_ms < 200, (
                f"cached {start} to {finish} took {cached_ms:.1f} ms"
            )
            assert external_calls <= 3, (
                f"cold {start} to {finish} request made "
                f"{external_calls} external calls"
            )
            print(
                f"| {start} to {finish} | {distance:.2f} | {stop_count} | "
                f"{float(cold_payload['total_fuel_cost_usd']):.2f} | "
                f"{largest_gap:.2f} | {cold_ms:.1f} | {cached_ms:.1f} | "
                f"{external_calls} | {cached_calls} |"
            )


if __name__ == "__main__":
    main()
