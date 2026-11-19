# Fuel Route API

Django REST API that plans a US driving route and selects **cost-optimal fuel stops** given a 500-mile tank range and 10 mpg efficiency.

## Features

- `POST /api/route/` and `GET /api/route/` — route geometry, fuel stops, total cost
- `GET /api/route/map/` — Leaflet map (OSM tiles, Esri fallback) with stop markers and a stops table
- Optional `starting_fuel_gallons` (0–50, default 50 = full tank)
- `MIN_PURCHASE_GALLONS` (default 5) and `RESERVE_MILES` (default 25) are configurable in `.env`
- Offline station geocoding at import time; in-memory numpy station index
- Cached geocode + route + full plan responses (warm → 0 external calls)
- At most 3 external HTTP calls on a cold request (≤2 Nominatim + 1 OSRM)
- DEBUG responses include `X-External-Calls`, counting actual upstream attempts (including retries)

## Quick start (Windows PowerShell)

Requires **Python 3.12+** (tested with Python 3.13). If `python --version` shows an older version, use the launcher:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -r requirements.txt
copy .env.example .env

python manage.py migrate
python manage.py import_stations
python manage.py runserver
```

macOS / Linux:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py import_stations
python manage.py runserver
```

Place the OPIS CSV at `data/fuel-prices.csv`. Offline city centroids ship as `data/us-cities.tsv` (~22k US places). Unmatched city/state pairs are written to `data/unmatched.csv` on each import.

## API

### `POST /api/route/` (also `GET` with query params)

| Field | Type | Notes |
|-------|------|--------|
| `start` | string | US city/address or `lat,lng` |
| `finish` | string | US city/address or `lat,lng` |
| `starting_fuel_gallons` | float | Optional, 0–50, default **50** (full tank) |

### Example — short trip

```powershell
Invoke-RestMethod -Method POST -Uri http://127.0.0.1:8000/api/route/ `
  -ContentType application/json `
  -Body '{"start":"Dallas, TX","finish":"Houston, TX"}'
```

### Example — cross-country

```powershell
Invoke-RestMethod -Method POST -Uri http://127.0.0.1:8000/api/route/ `
  -ContentType application/json `
  -Body '{"start":"New York, NY","finish":"Los Angeles, CA"}'
```

### Example response shape

```json
{
  "start": {"lat": 40.71, "lng": -74.01, "display_name": "New York, NY", "label": "New York, NY"},
  "finish": {"lat": 34.05, "lng": -118.24, "display_name": "Los Angeles, CA", "label": "Los Angeles, CA"},
  "total_distance_miles": 2793.9,
  "total_fuel_cost_usd": 698.93,
  "total_gallons": 229.41,
  "fuel_stops": [
    {
      "name": "SHEETZ #639",
      "city": "Youngstown",
      "state": "OH",
      "lat": 41.1,
      "lng": -80.65,
      "mile_marker": 383.0,
      "price_per_gallon": 3.06,
      "gallons_purchased": 4.65,
      "cost_usd": 14.23
    }
  ],
  "route": {"type": "LineString", "coordinates": [[-74.0, 40.7], "..."]},
  "map_url": "/api/route/map/?start=New+York%2C+NY&finish=Los+Angeles%2C+CA&starting_fuel_gallons=50",
  "assumptions": {
    "max_range_miles": 500,
    "mpg": 10,
    "starting_fuel_gallons": 50,
    "min_purchase_gallons": 5,
    "reserve_miles": 25,
    "note": "Cost counts only fuel purchased; the starting tank is assumed already paid for."
  }
}
```

Money (including price per gallon) is rounded to **2 decimals**; distances and gallons to **2 decimals**.

### Map

`http://127.0.0.1:8000/api/route/map/?start=New%20York,%20NY&finish=Los%20Angeles,%20CA`

Postman collection: [`docs/postman_collection.json`](docs/postman_collection.json) (`{{base_url}}` defaults to `http://127.0.0.1:8000`).

## Tests

```powershell
pytest -q
```

For live API checks, start the Django server in one PowerShell window and run this in another:

```powershell
python manage.py runserver
python scripts/verify_api.py
```

The verifier checks route fuel/range/cost invariants, cold and cached timing, and upstream call counts for New York–Los Angeles, Dallas–Houston, Seattle–Miami, and Chicago–Denver. `X-External-Calls` is emitted only when `DEBUG=True`.

## Design decisions

### Full starting tank (“arrive empty”)

- Max range **500 miles**, **10 mpg** → full tank = **50 gallons**.
- Keep a **25-mile reserve** (configurable via `RESERVE_MILES`), so the default effective travel range between fuel-ups is **475 miles**.
- Intermediate fuel purchases are at least **5 gallons** by default (configurable via `MIN_PURCHASE_GALLONS`) unless a smaller purchase is needed to reach the next feasible station or the destination is within usable range. If current fuel already reaches another station, the planner skips a below-minimum top-up. Final-destination purchases are also exempt.
- Default: vehicle **starts full**. Only gallons **purchased** along the route are summed into cost.
- Optional `starting_fuel_gallons` (0–50) models a partially full tank; the first stop must be reachable before that fuel runs out.
- At intermediate stops, a below-minimum purchase is allowed when needed to reach the next feasible station; the final destination leg may also use a smaller purchase.

### Station import

- **US + DC only**; Canadian/other rows dropped.
- **Dedupe by OPIS ID**: keep **lowest price**; ties keep the **first** row.
- Prices outside `[1.00, 8.00]` dropped.
- Offline geocode: city+state → centroid via `us-cities.tsv` / geonamescache. Normalization: casefold, strip parentheticals/punctuation, St→Saint, Ft→Fort, Mt→Mount, `&`→`and`, then **difflib** fuzzy match (cutoff 0.88) within the same state.
- Unmatched pairs → `data/unmatched.csv`. Import replaces the table (idempotent).

### Routing & geocoding

- OSRM: one `overview=full&geometries=geojson` call per OD pair.
- Nominatim for free-text that is not a simple US `City, ST` (descriptive User-Agent); skipped for `lat,lng` and offline city hits.
- Timeouts (~10s), one retry on transient errors; upstream failures → **502/503** JSON (never a stack trace).
- Cache keys normalize strip / collapse spaces / casefold. Full plan cached too.

### Optimizer

- Pure module `routing/services/optimizer.py` (no Django).
- Dynamic programming compares fuel levels at each route station, including the minimum-purchase and reserve constraints, and minimizes total purchase cost across the complete route.
- Intermediate purchases meet the configured minimum unless the destination is within usable range or no station can be reached with current fuel and a smaller purchase is needed to reach the next feasible station.
- A fixed-seed independent DP oracle checks 220 randomized route plans and their fuel, reserve, and range invariants.
- Gaps with no reachable station → clear **400**.

### Map tiles

- `SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"` plus a matching `<meta name="referrer">`.
- Primary tiles: OSM `tile.openstreetmap.org`; on `tileerror`, fall back once to Esri World Street Map.

### Performance

- Stations loaded once into numpy arrays; vectorized corridor filter.
- Per-stage INFO timings (`geocode`, `routing`, `stations`, `optimize`).
- Route geometry is simplified to a 0.25-mile deviation tolerance for compact
  responses; gzip middleware further reduces API and map payload transfer size.
- Cached plan responses target **&lt; 200 ms**.

## Limitations

- Station coordinates are **city centroids**, not curb addresses — mile markers are approximate.
- Public OSRM/Nominatim rate limits/uptime are outside our control.
- SQLite + LocMem cache suit the assessment; use Postgres/Redis for multi-process production.
- Corridor distance is geometric to the route line, not driving detour.

## Project layout

```
config/                 # settings, URLs
routing/
  models.py
  views.py / serializers.py / urls.py
  services/             # geocoding, routing_client, stations, optimizer, route_planner, city_index
  management/commands/import_stations.py
  templates/routing/map.html
data/fuel-prices.csv
data/us-cities.tsv
docs/postman_collection.json
```

## Stack

Django **6.1.3** + DRF, Python 3.12+, SQLite, numpy, geonamescache, requests, pytest-django.
