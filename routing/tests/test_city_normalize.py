"""Tests for offline city name normalization and lookup helpers."""

from __future__ import annotations

from routing.services.city_index import normalize_city


def test_normalize_st_saint():
    assert normalize_city("St. Louis") == "saint louis"
    assert normalize_city("Saint Louis") == "saint louis"


def test_normalize_ft_fort():
    assert normalize_city("Ft. Worth") == "fort worth"
    assert normalize_city("Fort Worth") == "fort worth"


def test_normalize_mt_mount():
    assert normalize_city("Mt. Pleasant") == "mount pleasant"


def test_normalize_ampersand_and_parens():
    assert normalize_city("Lake & Park (East)") == "lake and park"


def test_normalize_new_york_alias():
    assert normalize_city("New York") == "new york city"
    assert normalize_city("NYC") == "new york city"


def test_normalize_case_and_punctuation():
    assert normalize_city("  Dallas!! ") == "dallas"
