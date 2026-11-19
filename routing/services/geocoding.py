"""Geocoding: lat/lng passthrough, offline City/ST lookup, then Nominatim."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache

from routing.services.city_index import US_STATES, lookup_city_state
from routing.services.external_calls import count_external_call

logger = logging.getLogger(__name__)

_LATLNG_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$"
)
_CITY_STATE_RE = re.compile(
    r"^\s*(.+?)\s*,\s*([A-Za-z]{2}|[A-Za-z][A-Za-z .]+[A-Za-z])\s*$"
)

US_STATE_NAMES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}


class GeocodingError(Exception):
    """Client/input geocoding failure (maps to HTTP 400)."""


class GeocodingUpstreamError(Exception):
    """Upstream Nominatim failure (maps to HTTP 502/503)."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class GeoPoint:
    lat: float
    lng: float
    display_name: str
    label: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_query(value: str) -> str:
    """Strip, collapse whitespace, casefold — used for cache keys."""
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def parse_latlng(value: str) -> tuple[float, float] | None:
    """Return (lat, lng) if ``value`` looks like ``'lat,lng'``, else None."""
    match = _LATLNG_RE.match(value)
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def _cache_key(raw: str) -> str:
    digest = hashlib.sha256(normalize_query(raw).encode("utf-8")).hexdigest()[:32]
    return f"geocode:v3:{digest}"


def _in_usa_bbox(lat: float, lng: float) -> bool:
    box = settings.USA_BBOX
    return (
        box["min_lat"] <= lat <= box["max_lat"]
        and box["min_lng"] <= lng <= box["max_lng"]
    )


def _is_us_result(payload: dict[str, Any]) -> bool:
    address = payload.get("address") or {}
    country = (address.get("country_code") or payload.get("country_code") or "").lower()
    if country and country != "us":
        return False
    try:
        lat = float(payload["lat"])
        lng = float(payload["lon"])
    except (KeyError, TypeError, ValueError):
        return False
    return _in_usa_bbox(lat, lng)


def _resolve_state(token: str) -> str:
    token = token.strip()
    upper = token.upper()
    if upper in US_STATES:
        return upper
    return US_STATE_NAMES.get(token.casefold(), "")


def _offline_city_state(raw: str) -> GeoPoint | None:
    """Resolve ``City, ST`` / ``City, State`` via the offline city index."""
    match = _CITY_STATE_RE.match(raw)
    if not match:
        return None
    city, state_raw = match.group(1).strip(), match.group(2).strip()
    state = _resolve_state(state_raw)
    if not state:
        # Looks like City, Country (e.g. Toronto, Canada) — not USA.
        raise GeocodingError(f"Location is not in the USA: {raw!r}")
    coords = lookup_city_state(city, state)
    if coords is None:
        return None
    lat, lng = coords
    return GeoPoint(
        lat=lat,
        lng=lng,
        display_name=f"{city}, {state}",
        label=raw,
    )


def _nominatim_search(raw: str) -> list[dict[str, Any]]:
    url = f"{settings.NOMINATIM_BASE_URL}/search"
    params = {
        "q": raw,
        "format": "json",
        "limit": 1,
        "addressdetails": 1,
        "countrycodes": "us",
    }
    headers = {
        "User-Agent": settings.NOMINATIM_USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }
    timeout = float(settings.HTTP_TIMEOUT_SECONDS)
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            count_external_call()
            response = requests.get(url, params=params, headers=headers, timeout=timeout)
            if response.status_code == 429:
                raise GeocodingUpstreamError(
                    "Geocoding service rate-limited. Please retry shortly.",
                    status_code=503,
                )
            if response.status_code >= 500:
                if attempt == 0:
                    time.sleep(0.35)
                    continue
                raise GeocodingUpstreamError(
                    f"Geocoding service unavailable (HTTP {response.status_code}).",
                    status_code=502,
                )
            if response.status_code >= 400:
                raise GeocodingUpstreamError(
                    f"Geocoding service error (HTTP {response.status_code}).",
                    status_code=502,
                )
            return response.json()
        except GeocodingUpstreamError:
            raise
        except requests.Timeout as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(0.35)
                continue
            raise GeocodingUpstreamError(
                "Geocoding service timed out.", status_code=503
            ) from exc
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(0.35)
                continue
            raise GeocodingUpstreamError(
                f"Geocoding service error: {exc}", status_code=502
            ) from exc
    raise GeocodingUpstreamError(
        f"Geocoding service error: {last_exc}", status_code=502
    )


def geocode(query: str) -> GeoPoint:
    """
    Resolve ``query`` to a US lat/lng.

    Order: ``lat,lng`` → offline ``City, ST`` → cached Nominatim.
    """
    raw = re.sub(r"\s+", " ", (query or "").strip())
    if not raw:
        raise GeocodingError("Location must not be empty.")

    coords = parse_latlng(raw)
    if coords is not None:
        lat, lng = coords
        if not _in_usa_bbox(lat, lng):
            raise GeocodingError("Coordinates are outside the USA bounding box.")
        return GeoPoint(
            lat=lat,
            lng=lng,
            display_name=f"{lat:.5f},{lng:.5f}",
            label=raw,
        )

    key = _cache_key(raw)
    cached = cache.get(key)
    if cached:
        logger.info("geocode cache hit for %r", raw)
        return GeoPoint(**cached)

    offline = _offline_city_state(raw)
    if offline is not None:
        cache.set(key, offline.to_dict(), timeout=settings.GEOCODE_CACHE_TTL)
        return offline

    data = _nominatim_search(raw)
    if not data:
        raise GeocodingError(f"Could not geocode location: {raw!r}")

    hit = data[0]
    if not _is_us_result(hit):
        raise GeocodingError(f"Location is not in the USA: {raw!r}")

    point = GeoPoint(
        lat=float(hit["lat"]),
        lng=float(hit["lon"]),
        display_name=hit.get("display_name") or raw,
        label=raw,
    )
    cache.set(key, point.to_dict(), timeout=settings.GEOCODE_CACHE_TTL)
    return point
