# 🌱 FarmDirect — Farm to Fork, Direct

A full-stack digital marketplace connecting farmers & FPOs with consumers and bulk buyers, with
**mandi demand/price forecasting**, **smart supply aggregation** and **OR-Tools multi-depot route
optimisation**. UI and architecture follow the KrishiSetu sample (Flask + SQLite + Jinja; maps are
Leaflet over CARTO tiles with an offline SVG fallback); the ML, aggregation and routing engines are the team's own.

## Quick start

```bash
pip install -r requirements.txt
python seed/seed_db.py        # schema + demo data (Pune & Nagpur regions)
python ml/train_models.py     # load Agmarknet CSVs, train models, write 14-day forecasts
python app.py                 # -> http://localhost:8000
```

Re-run `train_models.py` before a demo: forecasts are generated for "the next 14 days from today".
Smoke test (all roles, ~50 checks; it changes demo data, so re-seed + retrain before running it again):
`FARMLINK_OSRM=0 python tests/smoke_test.py`

## Demo accounts (password `demo123`, one-click buttons on the login page)

| Account | Email | Try |
|---|---|---|
| Farmer (Pune) | `ramesh@fl.in` | confirm / pack orders, **AI Insights**, add a listing |
| FPO (Pune) | `fpo@fl.in` | aggregated listings |
| Farmer (Nagpur) | `deepak@fl.in` | Nagpur Orange orchard |
| Consumer | `consumer@fl.in` | shop → cart → checkout → **live tracking** |
| Bulk buyer (Pune) | `buyer@fl.in` | **Smart Sourcing**: 1200 kg Tomato → multi-farm plan |
| Bulk buyer (Nagpur) | `ocmart@fl.in` | Nagpur Orange sourcing |
| Logistics | `logistics@fl.in` | **Optimize** → route map → mark pickups / deliveries |
| Admin | `admin@fl.in` | KPIs, routing savings, model accuracy |

**Suggested 3-minute demo:** login as `logistics@fl.in` → see the pickup pool map → *Optimize all regions* →
switch vehicle chips / Nagpur depot → mark a pickup done → open the order's tracking page as its buyer.
Then `buyer@fl.in` → Smart Sourcing → *Place order* → farmer packs it → logistics optimizes again.

## What changed vs the sample

| Area | Sample (KrishiSetu) | FarmDirect |
|---|---|---|
| Forecasting | RandomForest on synthetic data | Team's RandomForest on real **Agmarknet** CSVs (`ml/data/`) |
| Bulk buying | Quote requests | + **Smart Sourcing** — ranking, farm-wise allocation, OR-Tools-priced pickup route |
| Routing | NN + 2-opt, one hub | **OR-Tools** capacity-aware pickup-and-delivery VRP, **per depot** (Pune, Nagpur) |
| Order flow | placed → … → delivered | + `assigned` (driver & route) and per-stop pickup / delivery mark-off |
| Regions | Pune | Marketplace filtered by region; cross-region carts are blocked |
| UI | shared template, emoji icons | Per-role app shell (own sidebar + accent colour per role), vendored Lucide icons + flat crop art (no emoji), Inter/Source Serif type, maps with a maximize toggle and a compact attribution chip |

## UI: per-role dashboards

Every signed-in role gets its own app shell (`templates/_app_base.html`) — a sidebar scoped to
that role's tasks, an accent colour (`--role-c` / `--role-soft` in `style.css`), and a landing
page tailored to the job, instead of everyone sharing the marketplace/orders pages:

| Role | Landing page | Sidebar |
|---|---|---|
| Consumer | `/dashboard` — active orders, savings, region picks | Dashboard · My orders · Cart · Marketplace |
| Farmer / FPO | `/farmer` (title switches to "FPO desk" for FPO farms) | Dashboard · My listings · AI insights · Marketplace |
| Bulk buyer | `/buyer` | Dashboard · Smart sourcing · My orders · Wholesale marketplace |
| Logistics | `/logistics` | Logistics console · Marketplace |
| Admin | `/admin` | Platform admin · Logistics · Marketplace |

Icons are vendored Lucide SVGs (`static/icons/`, via `icon()` in `app.py`) inlined so they take
`currentColor` and any size; produce gets small flat vector tiles (`crop_art()`) instead of emoji.
A `|deemoji` filter also strips any stray emoji from flashed messages as a safety net.

## Maps: maximize + compact attribution

Every Leaflet map (pickup pool, planned routes, tracking legs) gets a maximize button
(`static/js/main.js`, injected onto every `.map-wrap`) that opens it as a fullscreen overlay with
a dimmed scrim — click again, press Escape, or click outside to close. The default Leaflet
attribution control ("Leaflet | © OpenStreetMap © CARTO") was covering a good chunk of small
maps, so it's built with `attributionControl: false` plus a manual `prefix: false` control, and
CSS collapses the remaining copyright text to a small "i" chip that expands on hover.

## Project structure

```
app.py                  Flask app — routes, helpers, SVG route map
charts.py               SVG line/bar charts
seed/seed_db.py         schema + demo data (users, farms, listings, orders, fleet)
ml/
  data/*.csv            Agmarknet exports (drop more crops/districts here)
  data_processor.py     cleaning + features (date-parsing fix)
  model_trainer.py      RandomForest price+demand, recursive forecast, hold-out evaluation
  train_models.py       pipeline -> tables, models.pkl, metrics.json
  matching.py           supply matching + smart aggregation engine
routing/
  solver.py             OR-Tools VRP (team solver + optional shift cap / optional orders)
  matrix.py             OSRM distances with offline haversine fallback + circuit breaker
  planner.py            per-depot planning, persistence, stop mark-off, aggregation route estimate
tests/smoke_test.py
```

## Maps (CARTO)

Route maps use **Leaflet** (vendored in `static/vendor/leaflet/`, no CDN) over the **CARTO Voyager** basemap.
Paste your key in `app.py`:

```python
CARTO_API_KEY = os.getenv("CARTO_API_KEY", "")   # <- put your key between the quotes
```
(or `export CARTO_API_KEY=...`). It is sent as `?key=` on tile requests; empty = no key parameter.
If Leaflet or the tiles can't load (offline demo laptop), the page automatically shows the built-in SVG map instead.
Road-following lines need OSRM (see below); otherwise routes are drawn as dashed straight legs between stops.

## API

```
GET  /api/forecast/<crop>          14-day demand + price forecast (quintals, Rs/quintal)
POST /api/routes/optimize-all      {"depot_ids": [...]}  (logistics/admin session required)
```

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `CARTO_API_KEY` | *(empty)* | CARTO basemap key (or edit `app.py`) |
| `FARMLINK_OSRM` | `1` | `0` = never call OSRM (offline straight-line estimates) |
| `OSRM_URL` | public OSRM demo server | point at your own OSRM for production |
| `FARMLINK_MAX_SHIFT_MIN` | `600` | driver shift cap incl. handling time (`0` disables) |
| `FARMLINK_SOLVE_SECONDS` | `2` | OR-Tools search budget per depot |

## Honest notes / limitations

* **Forecast accuracy is modest** — ~100 daily rows per crop. Hold-out results are in `ml/models/metrics.json`
  and on the admin page, including a naive baseline. More history is the real fix.
* Only crops present in the CSVs (Tomato, Wheat) get forecasts; other crops show "no mandi data yet".
* Forecasts start from today while the CSV ends earlier, so lag features are a few weeks stale.
* Orders are split into **one shipment per farm**; one order may arrive on two vehicles.
* "Saved vs separate dispatch" compares against sending each order out as its own round trip.
* Without OSRM, distances are straight-line at 30 km/h (understates real road distance).
* Orders that cannot be scheduled within capacity / shift are reported and stay in the pickup pool.
* Prototype-grade: demo secret key, simulated payments, no CSRF protection.
* `static/img/hero.jpg` is taken from the sample — replace it with your own artwork.
