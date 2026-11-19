"""HTTP endpoints for route planning and the Leaflet map page."""

from __future__ import annotations

import logging

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from routing.serializers import RouteRequestSerializer
from routing.services.route_planner import RoutePlanError, plan_route

logger = logging.getLogger(__name__)


def _extract_params(request: Request) -> dict:
    if request.method == "GET":
        data = {
            "start": request.query_params.get("start", ""),
            "finish": request.query_params.get("finish", ""),
        }
        if "starting_fuel_gallons" in request.query_params:
            data["starting_fuel_gallons"] = request.query_params.get(
                "starting_fuel_gallons"
            )
        return data
    if isinstance(request.data, dict):
        data = {
            "start": request.data.get("start", ""),
            "finish": request.data.get("finish", ""),
        }
        if "starting_fuel_gallons" in request.data:
            data["starting_fuel_gallons"] = request.data.get("starting_fuel_gallons")
        return data
    return {"start": "", "finish": ""}


def _json_error(message: str | dict, status_code: int) -> JsonResponse:
    if isinstance(message, dict):
        body: dict = {"errors": message} if "errors" not in message else message
    else:
        body = {"error": message}
    return JsonResponse(body, status=status_code)


class RouteView(APIView):
    """POST/GET /api/route/ — plan a fuel-optimal driving route."""

    authentication_classes: list = []
    permission_classes: list = []

    def get(self, request: Request) -> Response:
        return self._handle(request)

    def post(self, request: Request) -> Response:
        return self._handle(request)

    def _handle(self, request: Request) -> Response:
        serializer = RouteRequestSerializer(data=_extract_params(request))
        if not serializer.is_valid():
            return Response(
                {"errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        start = serializer.validated_data["start"]
        finish = serializer.validated_data["finish"]
        fuel = serializer.validated_data["starting_fuel_gallons"]
        try:
            payload = plan_route(start, finish, starting_fuel_gallons=fuel)
        except RoutePlanError as exc:
            logger.warning("route plan failed: %s", exc)
            return Response({"error": str(exc)}, status=exc.status_code)
        except Exception:
            logger.exception("unexpected route plan failure")
            return Response(
                {"error": "Unable to plan route due to an unexpected error."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(payload)


class RouteMapView(APIView):
    """GET /api/route/map/ — Leaflet HTML page for the planned route."""

    authentication_classes: list = []
    permission_classes: list = []

    def get(self, request: Request) -> HttpResponse:
        serializer = RouteRequestSerializer(data=_extract_params(request))
        if not serializer.is_valid():
            return _json_error({"errors": serializer.errors}, 400)

        start = serializer.validated_data["start"]
        finish = serializer.validated_data["finish"]
        fuel = serializer.validated_data["starting_fuel_gallons"]
        try:
            payload = plan_route(start, finish, starting_fuel_gallons=fuel)
        except RoutePlanError as exc:
            return _json_error(str(exc), exc.status_code)
        except Exception:
            logger.exception("unexpected map plan failure")
            return _json_error(
                "Unable to plan route due to an unexpected error.", 502
            )

        context = {
            "start_label": payload["start"]["display_name"],
            "finish_label": payload["finish"]["display_name"],
            "total_distance": payload["total_distance_miles"],
            "total_cost": payload["total_fuel_cost_usd"],
            "route_geojson": payload["route"],
            "stops_data": payload["fuel_stops"],
            "start_data": payload["start"],
            "finish_data": payload["finish"],
        }
        return render(request, "routing/map.html", context)
