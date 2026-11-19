"""
Import and geocode fuel stations from data/fuel-prices.csv.

Offline geocoding uses ``data/us-cities.tsv`` when present (dense GeoNames
extract), otherwise geonamescache city centroids — no network calls.
Writes unmatched US city/state pairs to ``data/unmatched.csv``.
Idempotent: replaces the Station table on each run (no duplicate rows).
"""

from __future__ import annotations

import csv
import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routing.models import Station
from routing.services.city_index import US_STATES, get_city_index, lookup_city_state
from routing.services.stations import clear_station_cache

logger = logging.getLogger(__name__)

MIN_PRICE = Decimal("1.00")
MAX_PRICE = Decimal("8.00")


def parse_price(raw: str) -> Decimal | None:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError):
        return None
    if value < MIN_PRICE or value > MAX_PRICE:
        return None
    return value.quantize(Decimal("0.0001"))


class Command(BaseCommand):
    help = (
        "Import fuel stations from CSV, dedupe by OPIS ID (keep lowest price), "
        "filter to US states + DC, geocode offline via city centroids."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--csv",
            type=str,
            default=str(settings.FUEL_PRICES_CSV),
            help="Path to fuel-prices.csv",
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help="Delete existing Station rows before import (always done; kept for CLI compat)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        csv_path = Path(options["csv"])
        if not csv_path.exists():
            raise CommandError(f"CSV not found: {csv_path}")

        self.stdout.write(f"Reading {csv_path} ...")
        # Warm index (also used by lookup_city_state).
        city_index = get_city_index()
        self.stdout.write(f"Offline city index size: {len(city_index)}")

        stats = {
            "rows": 0,
            "deduped": 0,
            "non_us": 0,
            "bad_price": 0,
            "unmatched_city": 0,
            "imported": 0,
        }
        unmatched_pairs: dict[tuple[str, str], None] = {}
        best_by_id: dict[int, dict[str, Any]] = {}

        with csv_path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            required = {
                "OPIS Truckstop ID",
                "Truckstop Name",
                "Address",
                "City",
                "State",
                "Retail Price",
            }
            if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
                raise CommandError(
                    f"CSV missing required columns. Found: {reader.fieldnames}"
                )

            for row in reader:
                stats["rows"] += 1
                state = (row.get("State") or "").strip().upper()
                if state not in US_STATES:
                    stats["non_us"] += 1
                    continue

                price = parse_price(row.get("Retail Price") or "")
                if price is None:
                    stats["bad_price"] += 1
                    continue

                try:
                    opis_id = int(str(row.get("OPIS Truckstop ID") or "").strip())
                except ValueError:
                    stats["bad_price"] += 1
                    continue

                city = (row.get("City") or "").strip()
                coords = lookup_city_state(city, state)
                if coords is None:
                    stats["unmatched_city"] += 1
                    unmatched_pairs[(city, state)] = None
                    continue

                candidate = {
                    "opis_id": opis_id,
                    "name": (row.get("Truckstop Name") or "").strip()[:255],
                    "address": (row.get("Address") or "").strip()[:512],
                    "city": city[:128],
                    "state": state,
                    "price": price,
                    "lat": coords[0],
                    "lng": coords[1],
                }

                existing = best_by_id.get(opis_id)
                if existing is None:
                    best_by_id[opis_id] = candidate
                else:
                    stats["deduped"] += 1
                    if candidate["price"] < existing["price"]:
                        best_by_id[opis_id] = candidate

        stations = [Station(**payload) for payload in best_by_id.values()]
        stats["imported"] = len(stations)

        unmatched_path = Path(settings.BASE_DIR) / "data" / "unmatched.csv"
        unmatched_path.parent.mkdir(parents=True, exist_ok=True)
        with unmatched_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["city", "state"])
            writer.writeheader()
            for city, state in sorted(unmatched_pairs):
                writer.writerow({"city": city, "state": state})
        self.stdout.write(
            f"Wrote {len(unmatched_pairs)} unmatched city/state pairs to {unmatched_path}"
        )

        with transaction.atomic():
            Station.objects.all().delete()
            Station.objects.bulk_create(stations, batch_size=500)

        clear_station_cache()

        summary = (
            f"Import complete: rows={stats['rows']} imported={stats['imported']} "
            f"deduped={stats['deduped']} non_us={stats['non_us']} "
            f"bad_price={stats['bad_price']} unmatched_city={stats['unmatched_city']}"
        )
        self.stdout.write(self.style.SUCCESS(summary))
        logger.info(summary)
