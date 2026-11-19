"""Tests for the import_stations management command."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command

from routing.models import Station
from routing.services.city_index import get_city_index
from routing.services.stations import clear_station_cache


@pytest.fixture
def temporary_base_dir(tmp_path: Path, monkeypatch):
    get_city_index()
    monkeypatch.setattr(settings, "BASE_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def tiny_csv(tmp_path: Path) -> Path:
    path = tmp_path / "fuel-prices.csv"
    rows = [
        {
            "OPIS Truckstop ID": "1001",
            "Truckstop Name": "Alpha Stop",
            "Address": "1 Main",
            "City": "Dallas",
            "State": "TX",
            "Rack ID": "1",
            "Retail Price": "3.50",
        },
        {
            "OPIS Truckstop ID": "1001",
            "Truckstop Name": "Alpha Stop Alt",
            "Address": "1 Main",
            "City": "Dallas",
            "State": "TX",
            "Rack ID": "1",
            "Retail Price": "3.90",
        },
        {
            "OPIS Truckstop ID": "1001",
            "Truckstop Name": "Alpha Stop Cheap",
            "Address": "1 Main",
            "City": "Dallas",
            "State": "TX",
            "Rack ID": "1",
            "Retail Price": "3.10",
        },
        # Canadian / non-US state.
        {
            "OPIS Truckstop ID": "2002",
            "Truckstop Name": "Maple Stop",
            "Address": "9 North",
            "City": "Toronto",
            "State": "ON",
            "Rack ID": "2",
            "Retail Price": "3.20",
        },
        # Bad price outlier.
        {
            "OPIS Truckstop ID": "3003",
            "Truckstop Name": "Pricey",
            "Address": "2 Main",
            "City": "Austin",
            "State": "TX",
            "Rack ID": "3",
            "Retail Price": "99.99",
        },
        # Valid second station.
        {
            "OPIS Truckstop ID": "4004",
            "Truckstop Name": "Beta Stop",
            "Address": "3 Main",
            "City": "Austin",
            "State": "TX",
            "Rack ID": "4",
            "Retail Price": "3.25",
        },
        # Unmatched tiny town (US state but not in city index).
        {
            "OPIS Truckstop ID": "5005",
            "Truckstop Name": "Nowhere Stop",
            "Address": "4 Main",
            "City": "Zzqxytownville",
            "State": "TX",
            "Rack ID": "5",
            "Retail Price": "3.15",
        },
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "OPIS Truckstop ID",
                "Truckstop Name",
                "Address",
                "City",
                "State",
                "Rack ID",
                "Retail Price",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.mark.django_db
def test_import_stations_dedupes_and_filters(
    tiny_csv: Path, capsys, temporary_base_dir: Path
):
    clear_station_cache()
    call_command("import_stations", csv=str(tiny_csv), replace=True)

    stations = list(Station.objects.order_by("opis_id"))
    assert len(stations) == 2
    alpha = Station.objects.get(opis_id=1001)
    assert float(alpha.price) == pytest.approx(3.10)
    assert alpha.name == "Alpha Stop Cheap"
    assert alpha.lat != 0.0
    assert Station.objects.filter(opis_id=2002).count() == 0
    assert Station.objects.filter(opis_id=3003).count() == 0
    assert Station.objects.filter(opis_id=5005).count() == 0

    unmatched = temporary_base_dir / "data" / "unmatched.csv"
    assert unmatched.exists()
    with unmatched.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert any(r["city"] == "Zzqxytownville" and r["state"] == "TX" for r in rows)
    output = capsys.readouterr().out
    assert "deduped=2" in output
    assert "non_us=1" in output
    assert "unmatched_city=1" in output


@pytest.mark.django_db
def test_import_stations_idempotent(tiny_csv: Path, temporary_base_dir: Path):
    clear_station_cache()
    call_command("import_stations", csv=str(tiny_csv))
    call_command("import_stations", csv=str(tiny_csv))
    assert Station.objects.count() == 2
