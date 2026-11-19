"""Shared offline US city → lat/lng index (no network)."""

from __future__ import annotations

import csv
import difflib
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from django.conf import settings

US_STATES = {
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "DC",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
}

_CITY_ALIASES = {
    "st.": "saint",
    "st": "saint",
    "ft.": "fort",
    "ft": "fort",
    "mt.": "mount",
    "mt": "mount",
}

_CITY_NAME_ALIASES = {
    "new york": "new york city",
    "nyc": "new york city",
    "washington dc": "washington",
    "stl": "saint louis",
    "st louis": "saint louis",
}

_FUZZY_CUTOFF = 0.88


def normalize_city(name: str) -> str:
    """
    Normalize a city name for offline matching.

    Case-insensitive; strips parentheticals and punctuation; expands
    St./Ft./Mt.; maps ``&`` → ``and``.
    """
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold().strip()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    parts = [_CITY_ALIASES.get(token, token) for token in text.split()]
    norm = " ".join(parts)
    return _CITY_NAME_ALIASES.get(norm, norm)


def _load_from_tsv(path: Path) -> dict[tuple[str, str], tuple[float, float]]:
    best: dict[tuple[str, str], tuple[int, float, float]] = {}
    with path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            state = (row.get("state") or "").strip().upper()
            if state not in US_STATES:
                continue
            norm = normalize_city(row.get("name") or "")
            if not norm:
                continue
            try:
                pop = int(float(row.get("population") or 0))
                lat = float(row["lat"])
                lng = float(row["lng"])
            except (KeyError, TypeError, ValueError):
                continue
            key = (norm, state)
            prev = best.get(key)
            if prev is None or pop > prev[0]:
                best[key] = (pop, lat, lng)
    return {k: (v[1], v[2]) for k, v in best.items()}


def _load_from_geonamescache() -> dict[tuple[str, str], tuple[float, float]]:
    import geonamescache

    gc = geonamescache.GeonamesCache()
    best: dict[tuple[str, str], tuple[int, float, float]] = {}
    for city in gc.get_cities().values():
        if city.get("countrycode") != "US":
            continue
        admin = city.get("admin1code") or ""
        if admin not in US_STATES:
            continue
        norm = normalize_city(city.get("name") or "")
        if not norm:
            continue
        pop = int(city.get("population") or 0)
        key = (norm, admin)
        prev = best.get(key)
        if prev is None or pop > prev[0]:
            best[key] = (pop, float(city["latitude"]), float(city["longitude"]))
    return {k: (v[1], v[2]) for k, v in best.items()}


@lru_cache(maxsize=1)
def get_city_index() -> dict[tuple[str, str], tuple[float, float]]:
    """
    Prefer ``data/us-cities.tsv`` (dense GeoNames extract) when present;
    otherwise fall back to geonamescache (~3.4k US cities).
    """
    tsv = Path(settings.BASE_DIR) / "data" / "us-cities.tsv"
    if tsv.exists():
        return _load_from_tsv(tsv)
    return _load_from_geonamescache()


@lru_cache(maxsize=1)
def _names_by_state() -> dict[str, list[str]]:
    by_state: dict[str, list[str]] = {}
    for name, state in get_city_index():
        by_state.setdefault(state, []).append(name)
    return by_state


def lookup_city_state(city: str, state: str) -> tuple[float, float] | None:
    """
    Return (lat, lng) for a US city/state pair.

    Exact normalized match first, then difflib fuzzy match (cutoff 0.88)
    restricted to the same state.
    """
    state = (state or "").strip().upper()
    if state not in US_STATES:
        return None
    index = get_city_index()
    norm = normalize_city(city)
    if not norm:
        return None
    hit = index.get((norm, state))
    if hit is not None:
        return hit

    candidates = _names_by_state().get(state) or []
    matches = difflib.get_close_matches(norm, candidates, n=1, cutoff=_FUZZY_CUTOFF)
    if not matches:
        return None
    return index.get((matches[0], state))
