"""API view tests with geocoding/routing clients mocked."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from routing.models import Station
from routing.services.geo import simplify_route_coordinates
from routing.services.geocoding import GeocodingError, GeocodingUpstreamError, GeoPoint
from routing.services.routing_client import RouteResult, RoutingUpstreamError
from routing.services.stations import clear_station_cache


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def stations(db):
    clear_station_cache()
    cache.clear()
    Station.objects.bulk_create(
        [
            Station(
                opis_id=1,
                name="Near Start",
                address="1 A",
                city="Newark",
                state="NJ",
                price="3.20",
                lat=40.7357,
                lng=-74.1724,
            ),
            Station(
                opis_id=2,
                name="Mid Route",
                address="2 B",
                city="Harrisburg",
                state="PA",
                price="2.90",
                lat=40.2732,
                lng=-76.8867,
            ),
            Station(
                opis_id=3,
                name="Near End",
                address="3 C",
                city="Pittsburgh",
                state="PA",
                price="3.10",
                lat=40.4406,
                lng=-79.9959,
            ),
        ]
    )
    clear_station_cache()
    return None


def _fake_route(distance: float = 370.0) -> RouteResult:
    coords = [
        [-74.006, 40.7128],
        [-75.0, 40.5],
        [-76.0, 40.3],
        [-77.0, 40.3],
        [-78.0, 40.4],
        [-79.9959, 40.4406],
    ]
    return RouteResult(
        distance_miles=distance,
        duration_seconds=20000.0,
        geometry={"type": "LineString", "coordinates": coords},
        coordinates=coords,
    )


def test_simplify_route_reduces_collinear_points_and_preserves_endpoints():
    coordinates = [
        [0.0, 0.0],
        [0.005, 0.0],
        [0.010, 0.0],
        [0.015, 0.0],
    ]
    simplified = simplify_route_coordinates(coordinates)
    assert simplified[0] == coordinates[0]
    assert simplified[-1] == coordinates[-1]
    assert len(simplified) < len(coordinates)


@pytest.mark.django_db
def test_route_post_success(api, stations):
    with (
        patch(
            "routing.services.route_planner.geocode",
            side_effect=[
                GeoPoint(40.7128, -74.0060, "New York, NY", "New York, NY"),
                GeoPoint(40.4406, -79.9959, "Pittsburgh, PA", "Pittsburgh, PA"),
            ],
        ) as geocode_mock,
        patch(
            "routing.services.route_planner.fetch_route",
            return_value=_fake_route(),
        ) as route_mock,
    ):
        response = api.post(
            "/api/route/",
            {"start": "New York, NY", "finish": "Pittsburgh, PA"},
            format="json",
        )

    assert response.status_code == 200
    body = response.json()
    assert body["total_distance_miles"] == 370.0
    assert body["total_fuel_cost_usd"] == 0
    assert body["fuel_stops"] == []
    assert body["route"]["type"] == "LineString"
    assert "map_url" in body
    assert body["assumptions"]["starting_fuel_gallons"] == 50.0
    assert body["assumptions"]["min_purchase_gallons"] == 5.0
    assert body["assumptions"]["reserve_miles"] == 25.0
    assert geocode_mock.call_count == 2
    assert route_mock.call_count == 1


@pytest.mark.django_db
def test_route_missing_field(api):
    response = api.post("/api/route/", {"start": "Boston, MA"}, format="json")
    assert response.status_code == 400
    assert "errors" in response.json()


@pytest.mark.django_db
def test_route_identical_start_finish(api):
    response = api.post(
        "/api/route/",
        {"start": "Dallas, TX", "finish": "dallas, tx"},
        format="json",
    )
    assert response.status_code == 400
    assert "errors" in response.json()


@pytest.mark.django_db
def test_route_identical_geocoded_locations(api):
    with patch(
        "routing.services.route_planner.geocode",
        side_effect=[
            GeoPoint(32.7767, -96.7970, "Dallas, TX", "Dallas"),
            GeoPoint(32.7767, -96.7970, "Dallas, TX", "Dallas, Texas"),
        ],
    ), patch("routing.services.route_planner.fetch_route") as route_mock:
        response = api.post(
            "/api/route/",
            {"start": "Dallas", "finish": "Dallas, Texas"},
            format="json",
        )
    assert response.status_code == 400
    assert "same location" in response.json()["error"]
    route_mock.assert_not_called()


@pytest.mark.django_db
def test_route_non_us(api):
    with patch(
        "routing.services.route_planner.geocode",
        side_effect=GeocodingError("Location is not in the USA: 'Toronto, Canada'"),
    ):
        response = api.post(
            "/api/route/",
            {"start": "Toronto, Canada", "finish": "Houston, TX"},
            format="json",
        )
    assert response.status_code == 400
    assert "error" in response.json()


@pytest.mark.django_db
def test_route_upstream_failure(api):
    with patch(
        "routing.services.route_planner.geocode",
        side_effect=GeocodingUpstreamError(
            "Geocoding service timed out.", status_code=503
        ),
    ):
        response = api.post(
            "/api/route/",
            {"start": "New York, NY", "finish": "Boston, MA"},
            format="json",
        )
    assert response.status_code == 503
    assert "error" in response.json()


@pytest.mark.django_db
def test_route_osrm_upstream_failure(api, stations):
    with (
        patch(
            "routing.services.route_planner.geocode",
            side_effect=[
                GeoPoint(40.71, -74.0, "NY", "NY"),
                GeoPoint(42.36, -71.06, "BOS", "BOS"),
            ],
        ),
        patch(
            "routing.services.route_planner.fetch_route",
            side_effect=RoutingUpstreamError(
                "Routing service unavailable (HTTP 502).", status_code=502
            ),
        ),
    ):
        response = api.post(
            "/api/route/",
            {"start": "New York, NY", "finish": "Boston, MA"},
            format="json",
        )
    assert response.status_code == 502


@pytest.mark.django_db
def test_route_get_query_params(api, stations):
    with (
        patch(
            "routing.services.route_planner.geocode",
            side_effect=[
                GeoPoint(40.7128, -74.0060, "NY", "40.7128,-74.0060"),
                GeoPoint(40.4406, -79.9959, "PIT", "40.4406,-79.9959"),
            ],
        ),
        patch(
            "routing.services.route_planner.fetch_route",
            return_value=_fake_route(),
        ),
    ):
        response = api.get(
            "/api/route/",
            {
                "start": "40.7128,-74.0060",
                "finish": "40.4406,-79.9959",
                "starting_fuel_gallons": "50",
            },
        )
    assert response.status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "post"])
def test_starting_fuel_parameter_supported_for_get_and_post(api, stations, method):
    with (
        patch(
            "routing.services.route_planner.geocode",
            side_effect=[
                GeoPoint(40.7128, -74.0060, "New York, NY", "New York, NY"),
                GeoPoint(42.3601, -71.0589, "Boston, MA", "Boston, MA"),
            ],
        ),
        patch(
            "routing.services.route_planner.fetch_route",
            return_value=_fake_route(distance=60.0),
        ),
    ):
        if method == "get":
            response = api.get(
                "/api/route/",
                {
                    "start": "New York, NY",
                    "finish": "Boston, MA",
                    "starting_fuel_gallons": "10",
                },
            )
        else:
            response = api.post(
                "/api/route/",
                {
                    "start": "New York, NY",
                    "finish": "Boston, MA",
                    "starting_fuel_gallons": 10,
                },
                format="json",
            )
    assert response.status_code == 200
    assert response.json()["assumptions"]["starting_fuel_gallons"] == 10.0


@pytest.mark.django_db
def test_map_view_html(api, stations):
    with (
        patch(
            "routing.services.route_planner.geocode",
            side_effect=[
                GeoPoint(40.7128, -74.0060, "New York, NY", "New York, NY"),
                GeoPoint(40.4406, -79.9959, "Pittsburgh, PA", "Pittsburgh, PA"),
            ],
        ),
        patch(
            "routing.services.route_planner.fetch_route",
            return_value=_fake_route(),
        ),
    ):
        response = api.get(
            "/api/route/map/",
            {"start": "New York, NY", "finish": "Pittsburgh, PA"},
        )
    assert response.status_code == 200
    html = response.content.decode("utf-8")
    assert "leaflet" in html.lower()
    assert "tile.openstreetmap.org/{z}/{x}/{y}.png" in html
    assert "cartocdn" not in html.lower()
    assert 'name="referrer" content="strict-origin-when-cross-origin"' in html
    assert "return [Number(coordinate[1]), Number(coordinate[0])]" in html
    assert "map.fitBounds(routeLayer.getBounds(), { padding: [30, 30] })" in html
    assert 'window.addEventListener("load"' in html
    assert 'tr[data-stop-index]' in html


@pytest.mark.django_db
def test_map_view_validation_json(api):
    response = api.get("/api/route/map/", {"start": "Boston, MA"})
    assert response.status_code == 400
    assert response["Content-Type"].startswith("application/json")
    assert "errors" in response.json()
