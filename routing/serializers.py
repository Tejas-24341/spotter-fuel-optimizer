"""DRF serializers for the route API."""

from __future__ import annotations

import re

from django.conf import settings
from rest_framework import serializers


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(
        max_length=512,
        help_text='Free-text US address/city or "lat,lng"',
    )
    finish = serializers.CharField(
        max_length=512,
        help_text='Free-text US address/city or "lat,lng"',
    )
    starting_fuel_gallons = serializers.FloatField(
        required=False,
        default=None,
        help_text="Gallons in the tank at start (0–50). Default: full tank (50).",
    )

    def validate_start(self, value: str) -> str:
        value = _collapse(value)
        if not value:
            raise serializers.ValidationError("start must not be empty.")
        return value

    def validate_finish(self, value: str) -> str:
        value = _collapse(value)
        if not value:
            raise serializers.ValidationError("finish must not be empty.")
        return value

    def validate_starting_fuel_gallons(self, value: float | None) -> float:
        max_gal = float(settings.VEHICLE_MAX_RANGE_MILES) / float(settings.VEHICLE_MPG)
        if value is None:
            return max_gal
        if value < 0 or value > max_gal:
            raise serializers.ValidationError(
                f"starting_fuel_gallons must be between 0 and {max_gal:.0f}."
            )
        return float(value)

    def validate(self, attrs: dict) -> dict:
        start = attrs.get("start", "")
        finish = attrs.get("finish", "")
        if start and finish and start.casefold() == finish.casefold():
            raise serializers.ValidationError(
                {"finish": "start and finish must be different locations."}
            )
        if attrs.get("starting_fuel_gallons") is None:
            attrs["starting_fuel_gallons"] = float(
                settings.VEHICLE_MAX_RANGE_MILES
            ) / float(settings.VEHICLE_MPG)
        return attrs
