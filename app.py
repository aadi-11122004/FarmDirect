"""FarmDirect — farm-to-consumer digital marketplace (SIH prototype).

Flask app: marketplace, smart supply aggregation, multi-depot logistics with
OR-Tools route optimization, and an AI layer (mandi demand/price forecasting).
Run:  python app.py   ->  http://localhost:8000
"""

import json
import math
import os
import re
import uuid
from html import escape as html_escape
import sqlite3
from datetime import date, datetime, timedelta
from functools import lru_cache, wraps

from flask import (Flask, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

from charts import bar_chart, dual_forecast_chart, line_chart
from ml.matching import Listing, Requirement, create_aggregation, match_supply
from routing import planner
from routing.matrix import calculate_haversine

APP_NAME = "FarmDirect"

# ---- Map tiles: CARTO "Voyager" basemap (Leaflet). --------------------------------------
# Paste your CARTO API key between the quotes below (or set the CARTO_API_KEY env var).
# Left empty, tiles are requested without a key. If tiles / internet are unavailable the
# app automatically falls back to the built-in offline SVG map.
CARTO_API_KEY = os.getenv("CARTO_API_KEY", "cb1_3nty_1_a0030e47748741e05767b979")
CARTO_TILE_URL = "https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png"
BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "farmlink.db")
DELIVERY_FEE, FREE_ABOVE = 29, 499
STEPS = [("placed", "Order placed"), ("confirmed", "Farmer confirmed"),
         ("packed", "Packed · ready for pickup"), ("assigned", "Driver & route assigned"),
         ("in_transit", "Picked up · out for delivery"), ("delivered", "Delivered")]
REGION_CENTER = {}          # depot id -> (lat, lng), filled lazily

app = Flask(__name__)
app.secret_key = "farmlink-sih-demo-secret"

# ---- listing photo uploads: farmers/FPOs attach one image per listing ----
LISTING_UPLOAD_DIR = os.path.join(BASE, "static", "uploads", "listings")
os.makedirs(LISTING_UPLOAD_DIR, exist_ok=True)
ALLOWED_IMG_EXT = {"jpg", "jpeg", "png", "webp"}
MAX_IMG_BYTES = 5 * 1024 * 1024   # 5 MB


def save_listing_image(file_storage):
    """Save an uploaded crop photo under static/uploads/listings/ and return its
    url-relative path (e.g. 'uploads/listings/<uuid>.jpg'), or None if no valid
    file was given. Validates extension and size; never trusts the client name."""
    if not file_storage or not file_storage.filename:
        return None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in ALLOWED_IMG_EXT:
        return None
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size <= 0 or size > MAX_IMG_BYTES:
        return None
    fname = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(LISTING_UPLOAD_DIR, fname))
    return f"uploads/listings/{fname}"


# ----------------------------------------------------------------- database
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.before_request
def set_today():
    g.today = date.today()


def q(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


LSEL = """SELECT l.*, c.name AS crop_name, c.emoji, c.category,
        f.name AS farm_name, f.village, f.district, f.is_fpo, f.organic AS farm_organic,
        f.rating AS farm_rating, f.reliability AS farm_reliability, f.depot_id AS farm_depot, f.lat AS farm_lat, f.lng AS farm_lng,
        u.name AS farmer_name,
        ROUND((l.market_price - l.price) / l.market_price * 100, 1) AS save_pct
        FROM listings l
        JOIN crops c ON c.id = l.crop_id
        JOIN farms f ON f.id = l.farm_id
        LEFT JOIN users u ON u.id = f.user_id"""


# ------------------------------------------------------------------ helpers
def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    if "cu" not in g:
        g.cu = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    return g.cu


def login_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            u = current_user()
            if not u:
                flash("Please log in to continue.", "warn")
                return redirect(url_for("login", next=request.path))
            if roles and u["role"] not in roles:
                flash("That page isn't available for your account.", "warn")
                return redirect(url_for("index"))
            return fn(*a, **kw)
        return wrapper
    return deco


def inr(v):
    """Indian-style rupee formatting: ₹1,23,456.00"""
    try:
        v = round(float(v), 2)
    except (TypeError, ValueError):
        return "₹0"
    s = f"{v:,.2f}"
    whole, _, frac = s.partition(".")
    whole = whole.replace(",", "")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return f"₹{whole}.{frac}"


app.template_filter("inr")(inr)

# ---- Iconography: vendored Lucide SVGs (static/icons/*.svg), inlined so they can -----------
# take currentColor and sit at any size, instead of emoji glyphs that render inconsistently
# across platforms and read as "AI generated placeholder" rather than a designed product.
ICON_DIR = os.path.join(BASE, "static", "icons")
_ICON_ATTR_RE = re.compile(r'\s+width="24"|\s+height="24"')
_ICON_OPEN_RE = re.compile(r"<svg")


@lru_cache(maxsize=128)
def _icon_svg(name):
    path = os.path.join(ICON_DIR, f"{name}.svg")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return _ICON_ATTR_RE.sub("", fh.read())


def icon(name, size=18, cls=""):
    """Render a vendored Lucide icon inline. Falls back to a blank span if missing."""
    svg = _icon_svg(name)
    if not svg:
        return Markup(f"<span class='ico ico-missing {cls}' aria-hidden='true'></span>")
    svg = _ICON_OPEN_RE.sub(f'<svg width="{size}" height="{size}" class="ico {cls}" aria-hidden="true"', svg, count=1)
    return Markup(svg)


app.jinja_env.globals["icon"] = icon

# ---- Brand mark: one bold filled leaf glyph, used everywhere the app shows its logo ------
# (header, footer, favicon) so the mark is consistent rather than mixing a generic icon-set
# glyph with a separate one-off favicon path. A solid fill (not a thin stroke outline) reads
# clearly even at favicon size.
_BRAND_MARK_PATH = "M12 3c4 3 6 7 6 10.5 0 3-2.2 5.5-6 6.5-3.8-1-6-3.5-6-6.5C6 10 8 6 12 3Z"


def brand_mark(size=18, cls=""):
    return Markup(
        f"<svg width='{size}' height='{size}' viewBox='0 0 24 24' fill='currentColor' stroke='none' "
        f"class='ico {cls}' aria-hidden='true'><path d='{_BRAND_MARK_PATH}'/></svg>")


app.jinja_env.globals["brand_mark"] = brand_mark

# Flat, two-tone crop tiles (replace the sample's emoji produce icons). Each is a small
# hand-drawn vector on a soft rounded tile, built as a bold filled body (the crop's true
# colour, so the silhouette itself is recognisable) plus a couple of accent details (stem,
# leaves, eyes) — a single thin outline reads as an abstract blob at 28-40px, a filled shape
# with a recognisable silhouette does not. Each part is (mode, colour, path) where mode is
# "fill" or "stroke".
_CROP_ART = {
    "tomato": [
        ("fill", "#e0563f", "M12 8.5c-3.7 0-6.6 3-6.6 6.7S8.3 22 12 22s6.6-3 6.6-6.8-2.9-6.7-6.6-6.7Z"),
        ("fill", "#4c8c52", "M12 8.5c-.8-1.7-.8-3.4.1-4.7.9 1.3.9 3 .1 4.7ZM12 8.5c-2.1-.8-3.7-2-4.3-3.5 "
                             "1.8 0 3.4.9 4.3 3.5ZM12 8.5c2.1-.8 3.7-2 4.3-3.5-1.8 0-3.4.9-4.3 3.5Z"),
    ],
    "onion": [
        ("fill", "#c9832f", "M12 22c3.4 0 5.6-2.6 5.6-6.3 0-3.4-2-6.3-5.6-11.7-3.6 5.4-5.6 8.3-5.6 11.7"
                             "C6.4 19.4 8.6 22 12 22Z"),
        ("fill", "#7a5528", "M11.2 4c0-.5.4-.8.8-.8s.8.3.8.8v1.6h-1.6V4Z"),
        ("stroke", "#a3671f", "M9.2 13.6c1.2.7 4.4.7 5.6 0"),
        ("stroke", "#7a5528", "M9.6 19.4c-.3.6-.4 1.3-.2 2M12 20.2v1.4M14.4 19.4c.3.6.4 1.3.2 2"),
    ],
    "potato": [
        ("fill", "#b8823f", "M6.4 14.2c-.6-2.6.6-5 2.8-6.4 1.8-1.1 3.9-1 5.7 0 2.1 1.2 3.4 3.7 2.8 6.3"
                             "-.7 3.1-3.6 4.9-6.9 4.7-3-.2-5.6-1.8-4.4-4.6Z"),
        ("fill", "#7a5528", "M9.3 12.6a.7.7 0 1 1 0-1.4.7.7 0 0 1 0 1.4ZM13.5 11.3a.7.7 0 1 1 0-1.4"
                             ".7.7 0 0 1 0 1.4ZM11.4 15.1a.7.7 0 1 1 0-1.4.7.7 0 0 1 0 1.4Z"),
    ],
    "carrot": [
        ("fill", "#e0793f", "M13.3 8.2c2.6 2.8 2.7 9-1.6 13.4-2.7-2.8-3.6-6-3.4-8.4.3-3.3 2.6-5 5-5Z"),
        ("stroke", "#4c8c52", "M9.3 7.4 6.6 4.8M11.2 6.4 10 3.4M7.6 9.6 5.2 8M13.4 5.4c.4 1.7-.2 3-1.4 3.7"),
    ],
    "spinach": [
        ("fill", "#3f9e5c", "M12 21C6.9 19.7 4 15.7 4.3 10.8 9 10.4 12 12 12 12s3-1.6 7.7-1.2"
                             "C20 15.7 17.1 19.7 12 21Z"),
        ("stroke", "#2b6b3e", "M12 21V9.8"),
    ],
    "wheat": [
        ("stroke", "#c9a227", "M12 21V5.5"),
        ("fill", "#c9a227", "M9.4 6.4a1.7 1 -35 1 1 3-2.1 1.7 1 -35 1 1-3 2.1ZM14.6 6.4a1.7 1 35 1 0-3-2.1"
                             "1.7 1 35 1 0 3 2.1ZM9.4 9.6a1.7 1 -35 1 1 3-2.1 1.7 1 -35 1 1-3 2.1Z"
                             "M14.6 9.6a1.7 1 35 1 0-3-2.1 1.7 1 35 1 0 3 2.1ZM9.4 12.8a1.7 1 -35 1 1 3-2.1"
                             "1.7 1 -35 1 1-3 2.1ZM14.6 12.8a1.7 1 35 1 0-3-2.1 1.7 1 35 1 0 3 2.1Z"
                             "M12 3.7a1 1.6 0 1 1 0 3.2 1 1.6 0 0 1 0-3.2Z"),
    ],
    "banana": [
        ("fill", "#d9a520", "M6.6 17.6C5.3 15.2 5.9 11 9.5 8c.5-.4 1.1.1 1 .7-.9 4.3.6 8.1 4.5 9.5"
                             "2.5.9 5-.1 6-1.9.3-.5 1.1-.1.9.5-1.4 3.6-5.5 5.2-9.5 4.1C9.6 20.4 7.5 19.2 6.6 17.6Z"),
        ("stroke", "#8a6a3f", "M9.3 8.2c.3-.7 1-1.2 1.8-1.2"),
    ],
    "orange": [
        ("fill", "#e08a2e", "M12 20.6c4.8 0 8-3.6 8-8.6S16.8 3.4 12 3.4 4 7 4 12s3.2 8.6 8 8.6Z"),
        ("stroke", "#8a6a3f", "M12 4V2.2"),
        ("fill", "#4c8c52", "M12 2.6c1.6-.9 3.1-.5 3.6.4-1.6.9-3.1.5-3.6-.4Z"),
    ],
}
_CROP_ART_DEFAULT = [
    ("fill", "#3f7a4d", "M12 3c4 3 6 7 6 10.5 0 3-2.2 5.5-6 6.5-3.8-1-6-3.5-6-6.5C6 10 8 6 12 3Z"),
]


def crop_art(crop_name, size=40, cls=""):
    """A small flat vector tile for a crop, keyed off its name (falls back to a generic leaf).

    Each icon is a bold filled silhouette (so it reads at a glance) plus 1-2 accent strokes
    for the detail that actually distinguishes one crop from another (a tomato's calyx, a
    carrot's leaves, an onion's roots) rather than one generic thin outline for everything.
    """
    key = re.sub(r"[^a-z]", "", (crop_name or "").lower())
    parts = next((v for k, v in _CROP_ART.items() if k in key), _CROP_ART_DEFAULT)
    swatch = parts[0][1]
    body = []
    for mode, color, d in parts:
        if mode == "fill":
            body.append(f'<path d="{d}" fill="{color}"/>')
        else:
            body.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.3" '
                        f'stroke-linecap="round" stroke-linejoin="round"/>')
    return Markup(
        f'<span class="crop-art {cls}" style="--art-c:{swatch}" aria-hidden="true">'
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24">{"".join(body)}</svg></span>'
    )


app.jinja_env.globals["crop_art"] = crop_art

# Strip any leftover emoji from server-rendered text (flash messages, ad-hoc strings) so the
# UI stays consistent even if a message is composed dynamically elsewhere in the code.
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF☀-➿←-⇿⬀-⯿️]+"
)


def deemojify(s):
    if not isinstance(s, str):
        return s
    return _EMOJI_RE.sub("", s).strip()


app.template_filter("deemoji")(deemojify)


def all_depots():
    return [dict(d) for d in q("SELECT * FROM depots ORDER BY id")]


@app.context_processor
def inject_globals():
    cart = session.get("cart", {})
    return dict(user=current_user(), cart_count=sum(cart.values()), today=date.today(),
                STEPS=STEPS, APP_NAME=APP_NAME, CARTO_API_KEY=CARTO_API_KEY,
                CARTO_TILE_URL=CARTO_TILE_URL)


def nearest_depot_id(lat, lng):
    depots = all_depots()
    if not depots or lat is None or lng is None:
        return depots[0]["id"] if depots else None
    return planner.nearest_depot(depots, lat, lng)["id"]


def cart_rows():
    cart = session.get("cart", {})
    if not cart:
        return [], 0.0, 0.0
    ids = list(cart.keys())
    ph = ",".join("?" * len(ids))
    rows = [dict(r) for r in q(LSEL + f" WHERE l.id IN ({ph})", ids)]
    subtotal = savings = 0.0
    for r in rows:
        r["qty"] = float(cart[str(r["id"])])
        r["amount"] = round(r["price"] * r["qty"], 2)
        r["saved"] = round((r["market_price"] - r["price"]) * r["qty"], 2)
        subtotal += r["amount"]
        savings += r["saved"]
    return rows, round(subtotal, 2), round(savings, 2)


def region_conflicts(u, rows):
    """Items whose farm is served by a different depot than the buyer's region."""
    if not u or not u["depot_id"]:
        return []
    return [r for r in rows if r["farm_depot"] and r["farm_depot"] != u["depot_id"]]


def place_order(u, lines, address, slot, pay):
    """Create an order (+ items) from [(listing_id, qty)] lines. Returns order id."""
    subtotal = savings = 0.0
    prepared = []
    for lid, qty in lines:
        it = q(LSEL + " WHERE l.id=?", (lid,), one=True)
        if not it or qty <= 0:
            continue
        qty = min(float(qty), it["qty_available"])
        amt = round(it["price"] * qty, 2)
        subtotal += amt
        savings += round((it["market_price"] - it["price"]) * qty, 2)
        prepared.append((it, qty, amt))
    if not prepared:
        return None
    fee = 0 if subtotal >= FREE_ABOVE else DELIVERY_FEE
    lat, lng = u["lat"], u["lng"]
    depot_id = nearest_depot_id(lat, lng)
    cur = get_db().execute(
        "INSERT INTO orders(buyer_id,buyer_type,status,subtotal,savings,delivery_fee,total,payment_mode,"
        "address,lat,lng,depot_id,slot,placed_on,created_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (u["id"], "bulk" if u["role"] == "buyer" else "retail", "placed", round(subtotal, 2),
         round(savings, 2), fee, round(subtotal + fee, 2), pay, address, lat, lng, depot_id, slot,
         g.today.isoformat(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    oid = cur.lastrowid
    for it, qty, amt in prepared:
        farmer = q("SELECT user_id FROM farms WHERE id=?", (it["farm_id"],), one=True)["user_id"]
        get_db().execute(
            "INSERT INTO order_items(order_id,listing_id,farmer_id,crop_id,crop_name,qty,price,amount) "
            "VALUES(?,?,?,?,?,?,?,?)", (oid, it["id"], farmer, it["crop_id"], it["crop_name"], qty, it["price"], amt))
        get_db().execute("UPDATE listings SET qty_available = MAX(0, qty_available-?) WHERE id=?", (qty, it["id"]))
    get_db().commit()
    return oid


# ------------------------------------------------------------ AI / forecasts
def forecast_commodities():
    return [r["commodity"] for r in q("SELECT DISTINCT commodity FROM demand_forecast_logs ORDER BY commodity")]


def crop_insight(crop_name):
    """AI insight bundle for one crop (30-day history + 14-day forecast + outlook).
    Prices in Rs/kg, demand in quintals/day. None if no model is trained for the crop."""
    fc = [dict(r) for r in q(
        "SELECT forecast_date, predicted_demand_qty, predicted_price_qtl FROM demand_forecast_logs "
        "WHERE commodity=? ORDER BY forecast_date", (crop_name,))]
    if not fc:
        return None
    hist = [dict(r) for r in q(
        "SELECT recorded_date AS date, SUM(arrival_volume) AS demand_qtl, AVG(modal_price) AS price_qtl "
        "FROM market_price_historical WHERE commodity=? GROUP BY recorded_date "
        "ORDER BY recorded_date DESC LIMIT 30", (crop_name,))][::-1]
    if not hist:
        return None

    labels = [d["date"][5:] for d in hist] + [d["forecast_date"][5:] for d in fc]
    actual = [d["demand_qtl"] for d in hist] + [None] * len(fc)
    fdem = ([None] * (len(hist) - 1)) + [hist[-1]["demand_qtl"]] + [d["predicted_demand_qty"] for d in fc]
    p_actual = [d["price_qtl"] / 100 for d in hist] + [None] * len(fc)
    p_fc = ([None] * (len(hist) - 1)) + [hist[-1]["price_qtl"] / 100] + [d["predicted_price_qtl"] / 100 for d in fc]

    last7 = [d["demand_qtl"] for d in hist[-7:]]
    next7 = [d["predicted_demand_qty"] for d in fc[:7]]
    base = sum(last7) / len(last7) if last7 else 0
    outlook_pct = round((sum(next7) / len(next7) - base) / base * 100) if base else 0
    outlook = "high" if outlook_pct >= 8 else "low" if outlook_pct <= -8 else "stable"

    mandi_pred = sum(d["predicted_price_qtl"] for d in fc) / len(fc) / 100      # Rs/kg
    return dict(crop=crop_name, labels=labels, actual=actual, fdem=fdem, p_actual=p_actual, p_fc=p_fc,
                fc=fc, outlook=outlook, outlook_pct=outlook_pct, mandi_pred=mandi_pred,
                suggested=round(mandi_pred * 0.92 * 2) / 2, last_actual_date=hist[-1]["date"])


def forecast_chips():
    """AI demand-outlook chips for the landing page (crops that have a trained model)."""
    chips = []
    for name in forecast_commodities():
        ins = crop_insight(name)
        emoji = q("SELECT emoji FROM crops WHERE name=?", (name,), one=True)
        if ins:
            chips.append(dict(name=name, emoji=emoji["emoji"] if emoji else "🌱", pct=ins["outlook_pct"]))
    return chips


def model_metrics():
    try:
        with open(os.path.join(BASE, "ml", "models", "metrics.json")) as f:
            return json.load(f)["overall"]
    except Exception:
        return {}


# ---------------------------------------------------------------- route map
def route_map(depot, stops, geometry=None, highlight=None, km=None, pairs=None, road=False, label_all=True, w=760, h=420):
    """Inline SVG map (no tiles, works offline) of a delivery route.

    stops    : ordered list of {kind: pickup|drop, lat, lng, name, status?, order_id?}
    geometry : road polyline [[lat, lng], ...] from OSRM, used when road=True; else dashed straight legs
    highlight: order id whose stops get a red ring (tracking page)
    pairs    : [(pickup_xy, drop_xy)] to draw an *unplanned* pool instead of a route
    """
    if not stops and not pairs:
        return ""
    pts = [(depot["lat"], depot["lng"])] + [(s["lat"], s["lng"]) for s in stops]
    for p in (pairs or []):
        pts += [p[0], p[1]]
    geo = geometry if (road and geometry) else []
    if len(geo) > 500:
        step = len(geo) // 500 + 1
        geo = geo[::step] + [geo[-1]]
    pts += [(a, b) for a, b in geo]
    la0, la1 = min(p[0] for p in pts), max(p[0] for p in pts)
    lo0, lo1 = min(p[1] for p in pts), max(p[1] for p in pts)
    dla, dlo = max(la1 - la0, 0.02), max(lo1 - lo0, 0.02)
    la0, la1, lo0, lo1 = la0 - dla * .06, la1 + dla * .06, lo0 - dlo * .06, lo1 + dlo * .06
    kx = math.cos(math.radians((la0 + la1) / 2))
    pad = 44
    cw, ch = w - 2 * pad, h - 2 * pad - 14
    sx, sy = (lo1 - lo0) * kx, (la1 - la0)
    scale = min(cw / sx, ch / sy)
    ox, oy = pad + (cw - sx * scale) / 2, pad + (ch - sy * scale) / 2

    def X(lng): return ox + (lng - lo0) * kx * scale
    def Y(lat): return oy + (la1 - lat) * scale

    out = [f"<rect x='0' y='0' width='{w}' height='{h}' rx='14' fill='#eef3e9'/>"]
    for k in range(1, 8):
        out.append(f"<line x1='{w*k/8:.0f}' y1='10' x2='{w*k/8:.0f}' y2='{h-10}' stroke='#dce6d4' stroke-width='1'/>")
        out.append(f"<line x1='10' y1='{h*k/8:.0f}' x2='{w-10}' y2='{h*k/8:.0f}' stroke='#dce6d4' stroke-width='1'/>")

    hx, hy = X(depot["lng"]), Y(depot["lat"])
    if pairs:                                       # unplanned pool: pickup -> drop connectors
        for (pa, pb) in pairs:
            out.append(f"<line x1='{X(pa[1]):.1f}' y1='{Y(pa[0]):.1f}' x2='{X(pb[1]):.1f}' y2='{Y(pb[0]):.1f}' "
                       f"stroke='#1d63d8' stroke-opacity='.8' stroke-width='2' stroke-dasharray='5 5'/>")
        for (pa, pb) in pairs:
            out.append(f"<circle cx='{X(pa[1]):.1f}' cy='{Y(pa[0]):.1f}' r='6.5' fill='#43a047' stroke='#fff' stroke-width='2'/>")
            out.append(f"<circle cx='{X(pb[1]):.1f}' cy='{Y(pb[0]):.1f}' r='6.5' fill='#b5622f' stroke='#fff' stroke-width='2'/>")
    else:
        # route between each pickup (farm/FPO) and its delivery (consumer/bulk buyer), in blue.
        line_pts = geo if geo else ([[depot["lat"], depot["lng"]]] + [[s["lat"], s["lng"]] for s in stops]
                                    + [[depot["lat"], depot["lng"]]])
        loop = " ".join(f"{X(b):.1f},{Y(a):.1f}" for a, b in line_pts)
        dash = "" if geo else " stroke-dasharray='6 5'"
        out.append(f"<polyline points='{loop}' fill='none' stroke='#1d63d8' stroke-opacity='.85' "
                   f"stroke-width='3' stroke-linejoin='round' stroke-linecap='round'{dash}/>")
        for i, s in enumerate(stops, 1):
            x, y = X(s["lng"]), Y(s["lat"])
            done = s.get("status") == "done"
            fill = "#9a9a90" if done else ("#2f6b44" if s["kind"] == "pickup" else "#b5622f")
            txt = "#fff" if (done or s["kind"] == "pickup") else "#3e2723"
            ring = (f"<circle cx='{x:.1f}' cy='{y:.1f}' r='16' fill='none' stroke='#c62828' "
                    f"stroke-width='2.4'/>") if highlight and s.get("order_id") == highlight else ""
            label = s["name"].split(" · ")[-1] if s["kind"] == "pickup" else s["name"]
            out.append(f"{ring}<circle cx='{x:.1f}' cy='{y:.1f}' r='11' fill='{fill}' stroke='#fff' stroke-width='2'/>"
                       f"<text x='{x:.1f}' y='{y+3.6:.1f}' text-anchor='middle' font-size='10' "
                       f"font-weight='700' fill='{txt}'>{i}</text>")
            if label_all or (highlight and s.get("order_id") == highlight):
                out.append(f"<text x='{x:.1f}' y='{y+25:.1f}' text-anchor='middle' class='map-lab'>{label[:20]}</text>")

    out.append(f"<rect x='{hx-10:.1f}' y='{hy-10:.1f}' width='20' height='20' rx='5' fill='#12271a' stroke='#fff' stroke-width='2'/>"
               f"<text x='{hx:.1f}' y='{hy+4:.1f}' text-anchor='middle' font-size='11' fill='#fff' font-weight='700'>D</text>"
               f"<text x='{hx:.1f}' y='{hy+26:.1f}' text-anchor='middle' class='map-lab'>Depot</text>")
    out.append(f"<circle cx='22' cy='{h-16}' r='6' fill='#43a047'/><text x='32' y='{h-12}' class='map-lab'>Pickup (farm)</text>"
               f"<circle cx='128' cy='{h-16}' r='6' fill='#b5622f'/><text x='138' y='{h-12}' class='map-lab'>Delivery</text>")
    if km is not None:
        out.append(f"<text x='{w-14}' y='{h-12}' text-anchor='end' class='map-km'>route ≈ {km:.1f} km</text>")
    svg = f"<svg viewBox='0 0 {w} {h}' role='img'>" + "".join(out) + "</svg>"

    # Leaflet payload (rendered client-side over CARTO tiles; the SVG above is the offline fallback)
    line = geo if geo else ([[depot["lat"], depot["lng"]]] + [[s_["lat"], s_["lng"]] for s_ in stops]
                            + [[depot["lat"], depot["lng"]]])
    data = dict(
        depot=dict(lat=depot["lat"], lng=depot["lng"], name=depot.get("name", "Depot")),
        stops=[dict(kind=s_["kind"], lat=s_["lat"], lng=s_["lng"], name=s_["name"], seq=i,
                    status=s_.get("status", ""), order_id=s_.get("order_id"), qty=s_.get("qty"))
               for i, s_ in enumerate(stops, 1)],
        line=[] if pairs else line, road=bool(geo), highlight=highlight, km=km,
        pairs=[[list(a), list(b)] for a, b in (pairs or [])])
    payload = html_escape(json.dumps(data), quote=True)
    return Markup(f"<div class='lmap' data-map='{payload}'></div><div class='map-fallback'>{svg}</div>")


def timeline_for(order):
    if order["status"] == "cancelled":
        return []
    names = [s[0] for s in STEPS]
    try:
        idx = names.index(order["status"])
    except ValueError:
        idx = 0
    return [dict(key=k, label=lab, done=(i <= idx), current=(i == idx)) for i, (k, lab) in enumerate(STEPS)]


# ------------------------------------------------------------------- public
@app.route("/")
def index():
    stats = dict(
        farmers=q("SELECT COUNT(*) AS n FROM users WHERE role IN ('farmer')", one=True)["n"],
        fpos=q("SELECT COUNT(*) AS n FROM farms WHERE is_fpo=1", one=True)["n"],
        listings=q("SELECT COUNT(*) AS n FROM listings WHERE status='active' AND harvest_type='IMMEDIATE'", one=True)["n"],
        delivered=q("SELECT COUNT(*) AS n FROM orders WHERE status='delivered'", one=True)["n"],
        save_pct=q("SELECT ROUND(AVG(savings*100.0/subtotal),1) AS n FROM orders WHERE subtotal>0", one=True)["n"],
        regions=q("SELECT COUNT(*) AS n FROM depots", one=True)["n"],
    )
    premium = q("SELECT ROUND(AVG((market_price-price)*100.0/market_price),1) AS n FROM listings", one=True)["n"]
    featured = q(LSEL + " WHERE l.status='active' AND l.harvest_type='IMMEDIATE' "
                        "ORDER BY f.rating DESC, l.rating DESC LIMIT 6")
    return render_template("index.html", stats=stats, featured=featured, chips=forecast_chips(),
                           kpi_premium=premium)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        u = q("SELECT * FROM users WHERE email=?", (email,), one=True)
        if u and check_password_hash(u["password_hash"], pw):
            session["uid"] = u["id"]
            flash(f"Welcome back, {u['name']}!", "ok")
            nxt = request.args.get("next") or request.form.get("next")
            if nxt and nxt.startswith("/"):
                return redirect(nxt)
            return redirect(url_for({"farmer": "farmer_dash", "buyer": "buyer_dash",
                                     "logistics": "logistics", "admin": "admin",
                                     "consumer": "consumer_dash"}
                                    .get(u["role"], "marketplace")))
        flash("Invalid email or password.", "err")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    depots = all_depots()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        role = request.form.get("role", "consumer")
        if role not in ("farmer", "consumer", "buyer"):
            role = "consumer"
        depot = next((d for d in depots if str(d["id"]) == request.form.get("depot")), depots[0])
        if not name or not email or len(pw) < 4:
            flash("Please fill all fields (password ≥ 4 chars).", "err")
            return render_template("register.html", depots=depots)
        if q("SELECT id FROM users WHERE email=?", (email,), one=True):
            flash("That email is already registered.", "err")
            return render_template("register.html", depots=depots)
        cur = get_db().execute(
            "INSERT INTO users(name,email,password_hash,role,phone,address,lat,lng,depot_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (name, email, generate_password_hash(pw), role, request.form.get("phone", ""),
             request.form.get("address", ""), depot["lat"], depot["lng"], depot["id"]))
        if role == "farmer":
            village = request.form.get("village", "") or depot["name"].split(" ")[0]
            get_db().execute(
                "INSERT INTO farms(user_id,name,village,district,lat,lng,depot_id,organic,area_acres,rating,reliability) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (cur.lastrowid, f"{name}'s Farm", village, depot["name"].split(" ")[0],
                 depot["lat"], depot["lng"], depot["id"], 0, 2.0, 4.5, 80))
        get_db().commit()
        session["uid"] = cur.lastrowid
        flash(f"Welcome to {APP_NAME}!", "ok")
        return redirect(url_for({"farmer": "farmer_dash", "buyer": "buyer_dash"}.get(role, "marketplace")))
    return render_template("register.html", depots=depots)


@app.post("/logout")
def logout():
    session.clear()
    flash("Logged out. See you soon!", "ok")
    return redirect(url_for("index"))


# --------------------------------------------------------------- marketplace
@app.route("/marketplace")
def marketplace():
    u = current_user()
    if u and u["role"] == "logistics":
        flash("The marketplace isn't available for logistics accounts.", "warn")
        return redirect(url_for("logistics"))
    depots = all_depots()
    reg = request.args.get("reg")
    if reg is None:                                   # default to the shopper's own region
        reg = str(u["depot_id"]) if u and u["role"] in ("consumer", "buyer") and u["depot_id"] else "all"
    query = LSEL + " WHERE l.status='active' AND l.harvest_type='IMMEDIATE'"
    args = []
    if reg != "all" and reg.isdigit():
        query += " AND f.depot_id=?"
        args.append(int(reg))
    srch = request.args.get("q", "").strip()
    if srch:
        query += " AND (c.name LIKE ? OR f.name LIKE ? OR f.village LIKE ?)"
        args += [f"%{srch}%"] * 3
    cat = request.args.get("cat", "")
    if cat:
        query += " AND c.category=?"
        args.append(cat)
    if request.args.get("organic") == "1":
        query += " AND l.organic=1"
    if request.args.get("fpo") == "1":
        query += " AND f.is_fpo=1"
    sort = request.args.get("sort", "rating")
    query += {"price_asc": " ORDER BY l.price ASC", "price_desc": " ORDER BY l.price DESC",
              "saving": " ORDER BY save_pct DESC"}.get(sort, " ORDER BY f.rating DESC, l.rating DESC")
    items = q(query, args)
    cats = q("SELECT DISTINCT category FROM crops ORDER BY category")
    return render_template("marketplace.html", items=items, cats=cats, srch=srch, cat=cat, sort=sort,
                           organic=request.args.get("organic") == "1", fpo=request.args.get("fpo") == "1",
                           depots=depots, reg=reg)


@app.route("/product/<int:lid>")
def product(lid):
    u = current_user()
    if u and u["role"] == "logistics":
        flash("The marketplace isn't available for logistics accounts.", "warn")
        return redirect(url_for("logistics"))
    row = q(LSEL + " WHERE l.id=?", (lid,), one=True)
    if not row:
        flash("Listing not found.", "err")
        return redirect(url_for("marketplace"))
    item = dict(row)
    try:
        item["hdays"] = (g.today - date.fromisoformat(item["harvest_date"])).days
    except Exception:
        item["hdays"] = 0
    farm_crops = q("SELECT c.name, c.emoji FROM listings l JOIN crops c ON c.id=l.crop_id "
                   "WHERE l.farm_id=? AND l.status='active' LIMIT 5", (item["farm_id"],))
    ins = crop_insight(item["crop_name"])
    sug = dict(suggested_price=ins["suggested"]) if ins else None
    return render_template("product.html", item=item, farm_crops=farm_crops, sug=sug)


# ---------------------------------------------------------------------- cart
@app.post("/cart/add/<int:lid>")
def cart_add(lid):
    item = q(LSEL + " WHERE l.id=?", (lid,), one=True)
    if not item:
        flash("Listing not found.", "err")
        return redirect(url_for("marketplace"))
    u = current_user()
    if u and u["role"] not in ("consumer", "buyer"):
        flash("Only consumer and buyer accounts can add items to cart.", "warn")
        return redirect(request.referrer or url_for("marketplace"))
    if region_conflicts(u, [dict(item)]):
        flash(f"{item['farm_name']} serves a different region than your delivery address — "
              "pick a listing from your own region.", "warn")
        return redirect(request.referrer or url_for("marketplace"))
    cart = session.setdefault("cart", {})
    qty = request.form.get("qty", type=float) or item["min_order"]
    qty = max(qty, item["min_order"])
    qty = min(qty, item["qty_available"])
    cart[str(lid)] = round(qty, 1)
    session.modified = True
    flash(f"Added {qty:g} kg {item['crop_name']} to cart.", "ok")
    return redirect(request.referrer or url_for("marketplace"))


@app.post("/cart/update")
def cart_update():
    cart = session.get("cart", {})
    for key, val in request.form.items():
        if key.startswith("qty_") and key[4:].isdigit():
            try:
                v = max(float(val), 0)
            except ValueError:
                continue
            if v == 0:
                cart.pop(key[4:], None)
            else:
                cart[key[4:]] = round(v, 1)
    session.modified = True
    return redirect(url_for("cart"))


@app.post("/cart/remove/<int:lid>")
def cart_remove(lid):
    session.get("cart", {}).pop(str(lid), None)
    session.modified = True
    return redirect(url_for("cart"))


@app.route("/cart")
def cart():
    rows, subtotal, savings = cart_rows()
    fee = DELIVERY_FEE if (rows and subtotal < FREE_ABOVE) else 0
    return render_template("cart.html", rows=rows, subtotal=subtotal, savings=savings, fee=fee,
                           total=subtotal + fee)


# ------------------------------------------------------------------ checkout
@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    u = current_user()
    if not u:
        flash("Log in to place your order.", "warn")
        return redirect(url_for("login", next="/checkout"))
    if u["role"] not in ("consumer", "buyer"):
        flash("Only consumer/buyer accounts can place orders.", "warn")
        return redirect(url_for("marketplace"))
    rows, subtotal, savings = cart_rows()
    if not rows:
        flash("Your cart is empty.", "warn")
        return redirect(url_for("marketplace"))
    bad = region_conflicts(u, rows)
    if bad:
        flash("Remove items from other regions first: " + ", ".join(r["crop_name"] + " (" + r["farm_name"] + ")" for r in bad), "err")
        return redirect(url_for("cart"))
    fee = 0 if subtotal >= FREE_ABOVE else DELIVERY_FEE
    if request.method == "POST":
        address = request.form.get("address", "").strip() or u["address"] or "Pune"
        slot = request.form.get("slot", "9–11 AM")
        pay = request.form.get("payment", "UPI (demo)")
        oid = place_order(u, [(r["id"], r["qty"]) for r in rows], address, slot, pay)
        session["cart"] = {}
        session.modified = True
        flash("Order placed successfully!", "ok")
        return redirect(url_for("order_success", oid=oid))
    return render_template("checkout.html", rows=rows, subtotal=subtotal, savings=savings, fee=fee,
                           total=subtotal + fee, slots=["9–11 AM", "11 AM–1 PM", "4–7 PM"],
                           tomorrow=(date.today() + timedelta(days=1)).strftime("%a, %d %b"))


@app.route("/order/<int:oid>/success")
def order_success(oid):
    order = q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
    if not order:
        return redirect(url_for("marketplace"))
    items = q("SELECT * FROM order_items WHERE order_id=?", (oid,))
    return render_template("order_success.html", order=order, items=items)


@app.route("/orders")
def orders():
    u = current_user()
    if not u:
        return redirect(url_for("login", next="/orders"))
    if u["role"] == "farmer":
        return redirect(url_for("farmer_dash"))
    if u["role"] == "logistics":
        return redirect(url_for("logistics"))
    my = q("SELECT * FROM orders WHERE buyer_id=? ORDER BY id DESC LIMIT 30", (u["id"],))
    items = q("SELECT oi.* FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE o.buyer_id=?", (u["id"],))
    by_order = {}
    for it in items:
        by_order.setdefault(it["order_id"], []).append(it)
    return render_template("orders.html", orders=my, items=by_order)


@app.route("/dashboard")
@login_required("consumer")
def consumer_dash():
    u = current_user()
    my = q("SELECT * FROM orders WHERE buyer_id=? ORDER BY id DESC LIMIT 6", (u["id"],))
    active = [dict(o) for o in my if o["status"] not in ("delivered", "cancelled")]
    savings_row = q("SELECT ROUND(SUM(savings),0) AS s, COUNT(*) AS n FROM orders WHERE buyer_id=?",
                    (u["id"],), one=True)
    recent_crops = q(
        "SELECT DISTINCT oi.crop_name AS name FROM order_items oi JOIN orders o ON o.id=oi.order_id "
        "WHERE o.buyer_id=? ORDER BY oi.id DESC LIMIT 3", (u["id"],))
    depot_id = u["depot_id"]
    depot = q("SELECT * FROM depots WHERE id=?", (depot_id,), one=True)
    picks = q(LSEL + " WHERE l.status='active' AND l.harvest_type='IMMEDIATE' AND f.depot_id=? "
                     "ORDER BY l.rating DESC, save_pct DESC LIMIT 6", (depot_id,))
    return render_template("consumer/dashboard.html", orders=my, active=active, picks=picks, depot=depot,
                           saved_total=savings_row["s"] or 0, order_count=savings_row["n"] or 0,
                           recent_crops=[r["name"] for r in recent_crops])


@app.route("/track/<int:oid>")
def track(oid):
    order = q("SELECT o.*, u.name AS buyer_name FROM orders o JOIN users u ON u.id=o.buyer_id WHERE o.id=?",
              (oid,), one=True)
    if not order:
        flash("Order not found.", "err")
        return redirect(url_for("marketplace"))
    items = q("SELECT * FROM order_items WHERE order_id=?", (oid,))
    mine = [dict(r) for r in q(
        "SELECT d.*, v.code AS vehicle, v.vtype, v.driver_name, r.total_km, r.est_minutes, "
        "r.route_polyline, r.route_source, r.depot_id FROM deliveries d JOIN delivery_routes r ON r.id=d.route_id "
        "JOIN vehicles v ON v.id=d.vehicle_id WHERE d.order_id=? ORDER BY d.route_id, d.seq", (oid,))]
    legs = []
    for rid in dict.fromkeys(m["route_id"] for m in mine):
        my_stops = [m for m in mine if m["route_id"] == rid]
        all_stops = [dict(s) for s in q("SELECT * FROM deliveries WHERE route_id=? ORDER BY seq", (rid,))]
        depot = q("SELECT * FROM depots WHERE id=?", (my_stops[0]["depot_id"],), one=True)
        for s in all_stops:
            s["name"] = s["stop_name"]
        pos = {s["id"]: i + 1 for i, s in enumerate(all_stops)}
        legs.append(dict(
            vehicle=my_stops[0]["vehicle"], vtype=my_stops[0]["vtype"], driver=my_stops[0]["driver_name"],
            km=my_stops[0]["total_km"], mins=my_stops[0]["est_minutes"],
            my_stops=[dict(m, pos=pos[m["id"]]) for m in my_stops], n_stops=len(all_stops),
            map=route_map(dict(depot), all_stops, json.loads(my_stops[0]["route_polyline"] or "[]"),
                          highlight=oid, km=my_stops[0]["total_km"],
                          road=my_stops[0]["route_source"] == "osrm", label_all=False)))
    return render_template("track.html", order=order, items=items, steps=timeline_for(order), legs=legs)


# -------------------------------------------------------------------- farmer
@app.route("/farmer")
@login_required("farmer")
def farmer_dash():
    u = current_user()
    farm = q("SELECT * FROM farms WHERE user_id=?", (u["id"],), one=True)
    listings = q(LSEL + " WHERE l.farm_id=? ORDER BY l.id DESC", (farm["id"],)) if farm else []
    since30 = (date.today() - timedelta(days=30)).isoformat()
    earn30 = q("SELECT COALESCE(SUM(oi.amount),0) AS n FROM order_items oi "
               "JOIN orders o ON o.id=oi.order_id WHERE oi.farmer_id=? AND o.placed_on>=? "
               "AND o.status!='cancelled'", (u["id"], since30), one=True)["n"]
    earn_total = q("SELECT COALESCE(SUM(amount),0) AS n FROM order_items WHERE farmer_id=?",
                   (u["id"],), one=True)["n"]
    pending = q("SELECT DISTINCT o.* FROM orders o JOIN order_items oi ON oi.order_id=o.id "
                "WHERE oi.farmer_id=? AND o.status IN ('placed','confirmed','packed') "
                "ORDER BY o.id DESC", (u["id"],))
    recent = q("SELECT DISTINCT o.* FROM orders o JOIN order_items oi ON oi.order_id=o.id "
               "WHERE oi.farmer_id=? ORDER BY o.id DESC LIMIT 8", (u["id"],))
    days = [(date.today() - timedelta(days=k)) for k in range(13, -1, -1)]
    raw = q("SELECT o.placed_on AS d, SUM(oi.amount) AS v FROM order_items oi "
            "JOIN orders o ON o.id=oi.order_id WHERE oi.farmer_id=? AND o.placed_on>=? "
            "GROUP BY o.placed_on", (u["id"], days[0].isoformat()))
    m = {r["d"]: r["v"] for r in raw}
    earn_chart = line_chart([d.strftime("%d %b") for d in days],
                            [("Earnings ₹", "#245536", [m.get(d.isoformat(), 0) for d in days])], unit="₹")
    quotes = q("SELECT * FROM quotes WHERE status='open' ORDER BY id DESC LIMIT 5")
    order_items = q("SELECT oi.*, o.status AS ostatus FROM order_items oi "
                    "JOIN orders o ON o.id=oi.order_id WHERE oi.farmer_id=?", (u["id"],))
    items_by_order = {}
    for it in order_items:
        items_by_order.setdefault(it["order_id"], []).append(it)
    # pickups planned for this farm's orders (from the route optimizer)
    pickups = {}
    if farm:
        for r in q("SELECT d.order_id, d.eta_min, d.seq, v.code, v.driver_name FROM deliveries d "
                   "JOIN vehicles v ON v.id=d.vehicle_id JOIN delivery_routes rt ON rt.id=d.route_id "
                   "WHERE d.kind='pickup' AND d.status!='done' AND d.lat=? AND d.lng=?",
                   (farm["lat"], farm["lng"])):
            pickups[r["order_id"]] = r
    return render_template("farmer/dashboard.html", farm=farm, listings=listings, earn30=earn30,
                           earn_total=earn_total, pending=pending, recent=recent,
                           earn_chart=earn_chart, quotes=quotes, items=items_by_order, pickups=pickups)


@app.route("/farmer/listings")
@login_required("farmer")
def farmer_listings():
    u = current_user()
    farm = q("SELECT * FROM farms WHERE user_id=?", (u["id"],), one=True)
    listings = q(LSEL + " WHERE l.farm_id=? ORDER BY l.id DESC", (farm["id"],))
    crops = q("SELECT * FROM crops ORDER BY name")
    return render_template("farmer/listings.html", farm=farm, listings=listings, crops=crops)


@app.post("/farmer/listings/new")
@login_required("farmer")
def farmer_listing_new():
    u = current_user()
    farm = q("SELECT * FROM farms WHERE user_id=?", (u["id"],), one=True)
    crop_id = request.form.get("crop_id", type=int)
    qty = request.form.get("qty", type=float) or 100
    price = request.form.get("price", type=float) or 10
    organic = 1 if request.form.get("organic") else (farm["organic"] if farm else 0)
    grade = request.form.get("grade", "A") if request.form.get("grade") in ("A", "B", "C") else "A"
    expected = request.form.get("expected_date", "").strip()
    crop = q("SELECT * FROM crops WHERE id=?", (crop_id,), one=True)
    image_path = save_listing_image(request.files.get("image"))
    if crop and farm:
        htype = "EXPECTED" if expected and expected > g.today.isoformat() else "IMMEDIATE"
        get_db().execute(
            "INSERT INTO listings(farm_id,crop_id,qty_available,price,market_price,unit,grade,organic,"
            "min_order,harvest_type,harvest_date,status,rating,image_path) "
            "VALUES(?,?,?,?,?,'kg',?,?,?,?,?,'active',4.5,?)",
            (farm["id"], crop_id, max(qty, 1), price, crop["base_price"], grade, organic,
             max(request.form.get("min_order", type=float) or 1, 1), htype,
             expected if htype == "EXPECTED" else g.today.isoformat(), image_path))
        get_db().commit()
        flash(f"Listing created: {crop['name']} at {inr(price)}/kg"
              f"{' (expected harvest ' + expected + ')' if htype == 'EXPECTED' else ''}.", "ok")
    return redirect(url_for("farmer_listings"))


@app.post("/farmer/listings/<int:lid>/toggle")
@login_required("farmer")
def farmer_listing_toggle(lid):
    u = current_user()
    get_db().execute(
        "UPDATE listings SET status = CASE WHEN status='active' THEN 'paused' ELSE 'active' END "
        "WHERE id=? AND farm_id=(SELECT id FROM farms WHERE user_id=?)", (lid, u["id"]))
    get_db().commit()
    flash("Listing updated.", "ok")
    return redirect(url_for("farmer_listings"))


@app.post("/farmer/orders/<int:oid>/advance")
@login_required("farmer")
def farmer_order_advance(oid):
    u = current_user()
    has = q("SELECT id FROM order_items WHERE order_id=? AND farmer_id=?", (oid, u["id"]), one=True)
    order = q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
    nxt = {"placed": "confirmed", "confirmed": "packed"}.get(order["status"]) if order else None
    if has and nxt:
        get_db().execute("UPDATE orders SET status=? WHERE id=?", (nxt, oid))
        get_db().commit()
        flash(f"Order #{oid} → {nxt}.", "ok")
    return redirect(request.referrer or url_for("farmer_dash"))


@app.route("/farmer/insights")
@login_required("farmer")
def farmer_insights():
    u = current_user()
    farm = q("SELECT * FROM farms WHERE user_id=?", (u["id"],), one=True)
    my_crops = q("SELECT DISTINCT c.name, c.emoji FROM listings l JOIN crops c ON c.id=l.crop_id "
                 "WHERE l.farm_id=? AND l.status='active'", (farm["id"],)) if farm else []
    modelled = set(forecast_commodities())
    sel = request.args.get("crop", type=str) or next((c["name"] for c in my_crops if c["name"] in modelled), None)
    ins = crop_insight(sel) if sel else None
    forecast_chart = ""
    peak_demand_date = peak_demand_qty = avg_predicted_price = None
    if ins:
        fc = ins["fc"]
        forecast_chart = dual_forecast_chart(
            [d["forecast_date"][5:] for d in fc],
            [round(d["predicted_price_qtl"], 1) for d in fc],
            [round(d["predicted_demand_qty"], 1) for d in fc])
        peak = max(fc, key=lambda d: d["predicted_demand_qty"])
        peak_demand_date, peak_demand_qty = peak["forecast_date"], round(peak["predicted_demand_qty"])
        avg_predicted_price = round(sum(d["predicted_price_qtl"] for d in fc) / len(fc), 2)
    rows = []
    for c in my_crops:
        ci = crop_insight(c["name"])
        mine = q("SELECT MIN(price) AS p FROM listings WHERE farm_id=? AND crop_id="
                 "(SELECT id FROM crops WHERE name=?) AND status='active'", (farm["id"], c["name"]), one=True)
        rows.append(dict(crop=c["name"], emoji=c["emoji"], modelled=bool(ci),
                         outlook=ci["outlook"] if ci else None, pct=ci["outlook_pct"] if ci else None,
                         suggested=ci["suggested"] if ci else None, mine=mine["p"] if mine else None))
    return render_template("farmer/insights.html", my_crops=my_crops, modelled=modelled, sel=sel, ins=ins,
                           forecast_chart=forecast_chart, rows=rows,
                           peak_demand_date=peak_demand_date, peak_demand_qty=peak_demand_qty,
                           avg_predicted_price=avg_predicted_price)


# --------------------------------------------------------------------- buyer
@app.route("/buyer")
@login_required("buyer")
def buyer_dash():
    u = current_user()
    since30 = (date.today() - timedelta(days=30)).isoformat()
    agg = q("SELECT COUNT(*) AS n_orders, COALESCE(SUM(total),0) AS spend, "
            "COALESCE(SUM(savings),0) AS saved FROM orders WHERE buyer_id=? AND placed_on>=?",
            (u["id"], since30), one=True)
    my = q("SELECT * FROM orders WHERE buyer_id=? ORDER BY id DESC LIMIT 10", (u["id"],))
    top = q("SELECT crop_name, SUM(qty) AS kg FROM order_items oi JOIN orders o ON o.id=oi.order_id "
            "WHERE o.buyer_id=? GROUP BY crop_name ORDER BY kg DESC LIMIT 6", (u["id"],))
    top_chart = bar_chart([r["crop_name"].split(" (")[0] for r in top], [r["kg"] for r in top],
                          "#4f8a63", unit=" kg")
    myquotes = q("SELECT * FROM quotes WHERE contact=? ORDER BY id DESC", (u["email"],))
    crops = q("SELECT name FROM crops ORDER BY name")
    return render_template("buyer/dashboard.html", agg=agg, orders=my, top_chart=top_chart,
                           quotes=myquotes, crops=crops)


@app.post("/buyer/quote")
@login_required("buyer")
def buyer_quote():
    u = current_user()
    get_db().execute(
        "INSERT INTO quotes(buyer_name,org,crop_name,qty_kg,contact,note) VALUES(?,?,?,?,?,?)",
        (u["name"], u["name"], request.form.get("crop", ""), request.form.get("qty", type=float) or 100,
         u["email"], request.form.get("note", "")))
    get_db().commit()
    flash("Bulk quote request sent to FPOs & farmers.", "ok")
    return redirect(url_for("buyer_dash"))


def _aggregate_inputs(args):
    """Parse the smart-sourcing form (GET or POST) into a Requirement (or None)."""
    u = current_user()
    crop = (args.get("crop") or "").strip()
    if not crop:
        return None
    try:
        return Requirement(
            commodity=crop, quantity_kg=max(float(args.get("qty") or 0), 1),
            quality_grade=args.get("grade", "A") if args.get("grade") in ("A", "B", "C") else "A",
            required_date=args.get("by") or (g.today + timedelta(days=2)).isoformat(),
            delivery_lat=u["lat"], delivery_lng=u["lng"],
            max_price_per_kg=float(args["max_price"]) if args.get("max_price") else None,
            max_distance_km=float(args.get("max_dist") or 150))
    except ValueError:
        return None


def _listings_for(crop):
    rows = q(LSEL + " WHERE l.status='active' AND c.name=?", (crop,))
    return [Listing(listing_id=str(r["id"]), farmer_id=str(r["farm_id"]), farmer_name=r["farm_name"],
                    commodity=r["crop_name"], available_quantity_kg=r["qty_available"],
                    price_per_kg=r["price"], quality_grade=r["grade"], harvest_date=r["harvest_date"],
                    latitude=r["farm_lat"], longitude=r["farm_lng"], harvest_type=r["harvest_type"],
                    reliability_score=r["farm_reliability"],
                    extra=dict(village=r["village"], is_fpo=r["is_fpo"], organic=r["organic"]))
            for r in rows]


def _aggregation_plan(req, u):
    matched = match_supply(req, _listings_for(req.commodity))
    plan = create_aggregation(
        req, matched["candidates"],
        route_estimator=lambda allocs: planner.estimate_pickup_route(get_db(), allocs, u["lat"], u["lng"], u["address"]))
    return matched, plan


@app.route("/buyer/aggregate")
@login_required("buyer")
def buyer_aggregate():
    u = current_user()
    crops = q("SELECT name, emoji FROM crops ORDER BY name")
    req = _aggregate_inputs(request.args)
    matched = plan = rmap = None
    if req:
        matched, plan = _aggregation_plan(req, u)
        if plan["route"]:
            r = plan["route"]
            rmap = route_map(r["depot"], [dict(s, status="") for s in r["stops"]], km=r["km"])
    return render_template("buyer/aggregate.html", crops=crops, req=req, form=request.args, matched=matched,
                           plan=plan, rmap=rmap,
                           default_date=(g.today + timedelta(days=2)).isoformat())


@app.post("/buyer/aggregate/order")
@login_required("buyer")
def buyer_aggregate_order():
    u = current_user()
    req = _aggregate_inputs(request.form)
    if not req:
        flash("Please run a search first.", "warn")
        return redirect(url_for("buyer_aggregate"))
    _matched, plan = _aggregation_plan(req, u)      # recompute server-side; never trust hidden fields
    lines = [(int(a["listing_id"]), a["quantity_kg"]) for a in plan["allocations"]]
    oid = place_order(u, lines, u["address"] or "Pune", "9–11 AM", "UPI (demo)")
    if not oid:
        flash("Nothing to order — no eligible supply.", "warn")
        return redirect(url_for("buyer_aggregate"))
    flash(f"Aggregated order placed: {plan['allocated_quantity_kg']:g} kg {req.commodity} from "
          f"{plan['pickup_count']} farm(s).", "ok")
    return redirect(url_for("order_success", oid=oid))


# ----------------------------------------------------------------- logistics
@app.route("/logistics")
@login_required("logistics", "admin")
def logistics():
    depots = all_depots()
    did = request.args.get("d", type=int) or depots[0]["id"]
    depot = next((d for d in depots if d["id"] == did), depots[0])
    did = depot["id"]
    today = g.today.isoformat()
    vehicles = [dict(v) for v in q("SELECT * FROM vehicles WHERE depot_id=? ORDER BY id", (did,))]
    routes = [dict(r) for r in q(
        "SELECT r.*, v.code, v.vtype, v.driver_name, v.capacity_kg FROM delivery_routes r "
        "JOIN vehicles v ON v.id=r.vehicle_id WHERE r.depot_id=? AND r.rdate=? ORDER BY r.id", (did, today))]
    rid = request.args.get("r", type=int)
    route = next((r for r in routes if r["id"] == rid), None) \
        or next((r for r in routes if r["status"] != "completed"), None) or (routes[0] if routes else None)

    stops, rmap = [], ""
    if route:
        stops = [dict(s) for s in q(
            "SELECT d.*, o.status AS ostatus FROM deliveries d LEFT JOIN orders o ON o.id=d.order_id "
            "WHERE d.route_id=? ORDER BY d.seq", (route["id"],))]
        for s in stops:
            s["name"] = s["stop_name"]
            s["pickup_pending"] = q("SELECT COUNT(*) AS n FROM deliveries WHERE order_id=? AND kind='pickup' "
                                    "AND status!='done'", (s["order_id"],), one=True)["n"] > 0
        rmap = route_map(depot, stops, json.loads(route["route_polyline"] or "[]"), km=route["total_km"],
                         road=route["route_source"] == "osrm")

    pool = planner.load_pool(get_db(), did)
    pool_orders = {}
    for p in pool:
        o = pool_orders.setdefault(p["order_id"], dict(order_id=p["order_id"], slot=p["slot"], kg=0,
                                                       area=planner.area_of(p["d_address"]), crops=[], farms=[]))
        o["kg"] += p["qty"]
        o["crops"].append(p["crop_name"])
        o["farms"].append(p["village"])
    naive_est = 0.0
    for oid in pool_orders:                            # quick straight-line estimate: one trip per order
        sh = [p for p in pool if p["order_id"] == oid]
        leg = [(depot["lat"], depot["lng"])] + [(p["p_lat"], p["p_lng"]) for p in sh] + \
              [(sh[0]["d_lat"], sh[0]["d_lng"]), (depot["lat"], depot["lng"])]
        naive_est += sum(calculate_haversine(a[0], a[1], b[0], b[1]) for a, b in zip(leg[:-1], leg[1:])) / 1000
    if not route and pool:
        rmap = route_map(depot, [], pairs=[((p["p_lat"], p["p_lng"]), (p["d_lat"], p["d_lng"])) for p in pool])

    kpi = dict(pool=len(pool_orders), pool_kg=round(sum(o["kg"] for o in pool_orders.values())),
               free=sum(1 for v in vehicles if v["status"] == "idle"), fleet=len(vehicles),
               routes=len(routes), km=round(sum(r["total_km"] for r in routes), 1),
               saved=round(sum(r["naive_km"] - r["total_km"] for r in routes), 1),
               naive=round(sum(r["naive_km"] for r in routes), 1))
    return render_template("logistics/dashboard.html", depots=depots, depot=depot, vehicles=vehicles,
                           routes=routes, route=route, stops=stops, pool=list(pool_orders.values()),
                           naive_est=round(naive_est, 1), kpi=kpi, map=rmap,
                           shift_h=round(planner.MAX_SHIFT_MIN / 60, 1))


@app.post("/logistics/optimize")
@login_required("logistics", "admin")
def logistics_optimize():
    scope = request.form.get("scope", "one")
    did = request.form.get("d", type=int)
    res = planner.optimize(get_db(), None if scope == "all" else [did])
    if res["routes"]:
        src = {p["matrix_source"] for p in res["plans"] if p["matrix_source"]}
        flash(f"OR-Tools planned {res['routes']} route(s) for {res['orders']} order(s): "
              f"{res['km']} km vs {res['naive_km']} km if dispatched separately — "
              f"saved {res['saved_km']} km ({res['saved_pct']}%)"
              f"{' · offline distance estimate' if 'haversine' in src else ' · live road distances'}.", "ok")
    else:
        flash("Nothing to plan — no packed orders waiting, or no free vehicle in this depot.", "warn")
    for u_ in res["unassigned"]:
        flash(f"Order #{u_['order_id']} ({u_['depot']}) left unassigned: {u_['reason']}.", "warn")
    return redirect(url_for("logistics", d=did))


@app.post("/logistics/reset")
@login_required("logistics", "admin")
def logistics_reset():
    n = planner.reset_plan(get_db())
    flash(f"Plan reset ({n} route(s) removed) — orders are back in the pickup pool. (demo)", "warn")
    return redirect(url_for("logistics", d=request.form.get("d", type=int)))


@app.post("/logistics/stop/<int:sid>/done")
@login_required("logistics", "admin")
def logistics_stop_done(sid):
    s = planner.mark_stop_done(get_db(), sid)
    if s:
        flash("Pickup confirmed" if s["kind"] == "pickup" else "Delivered", "ok")
    return redirect(request.referrer or url_for("logistics"))


# --------------------------------------------------------------------- admin
@app.route("/admin")
@login_required("admin")
def admin():
    kpi = dict(
        farmers=q("SELECT COUNT(*) AS n FROM users WHERE role='farmer'", one=True)["n"],
        fpos=q("SELECT COUNT(*) AS n FROM farms WHERE is_fpo=1", one=True)["n"],
        consumers=q("SELECT COUNT(*) AS n FROM users WHERE role='consumer'", one=True)["n"],
        buyers=q("SELECT COUNT(*) AS n FROM users WHERE role='buyer'", one=True)["n"],
        orders=q("SELECT COUNT(*) AS n FROM orders", one=True)["n"],
        delivered=q("SELECT COUNT(*) AS n FROM orders WHERE status='delivered'", one=True)["n"],
        save_pct=q("SELECT ROUND(AVG(savings*100.0/subtotal),1) AS n FROM orders WHERE subtotal>0", one=True)["n"],
        premium=q("SELECT ROUND(AVG((market_price-price)*100.0/market_price),1) AS n FROM listings", one=True)["n"],
    )
    since30 = (date.today() - timedelta(days=30)).isoformat()
    kpi["gmv30"] = q("SELECT COALESCE(SUM(total),0) AS n FROM orders WHERE placed_on>=? "
                     "AND status!='cancelled'", (since30,), one=True)["n"]
    rt = q("SELECT COUNT(*) AS n, COALESCE(SUM(total_km),0) AS km, COALESCE(SUM(naive_km),0) AS naive "
           "FROM delivery_routes", one=True)
    kpi.update(routes=rt["n"], route_km=round(rt["km"], 1), route_saved=round(rt["naive"] - rt["km"], 1),
               route_pct=round((rt["naive"] - rt["km"]) / rt["naive"] * 100, 1) if rt["naive"] else 0,
               regions=q("SELECT COUNT(*) AS n FROM depots", one=True)["n"])

    days = [(date.today() - timedelta(days=k)) for k in range(13, -1, -1)]
    raw = q("SELECT placed_on AS d, SUM(total) AS v FROM orders WHERE placed_on>=? "
            "AND status!='cancelled' GROUP BY placed_on", (days[0].isoformat(),))
    m = {r["d"]: r["v"] for r in raw}
    gmv_chart = line_chart([d.strftime("%d %b") for d in days],
                           [("GMV ₹", "#245536", [m.get(d.isoformat(), 0) for d in days])], unit="₹")
    top = q("SELECT crop_name, SUM(qty) AS kg FROM order_items GROUP BY crop_name ORDER BY kg DESC LIMIT 8")
    top_chart = bar_chart([r["crop_name"].split(" (")[0] for r in top], [r["kg"] for r in top],
                          "#a9822a", unit=" kg")
    fc = []
    for name in forecast_commodities():
        ci = crop_insight(name)
        if ci:
            fc.append(dict(name=name, sug=ci["suggested"], mandi=ci["mandi_pred"],
                           outlook=ci["outlook"], pct=ci["outlook_pct"]))
    return render_template("admin/dashboard.html", kpi=kpi, gmv_chart=gmv_chart, top_chart=top_chart,
                           fc=fc, metrics=model_metrics())


# ----------------------------------------------------------------------- API
@app.get("/api/forecast/<path:crop>")
def api_forecast(crop):
    ins = crop_insight(crop)
    if not ins:
        return jsonify(error=f"no forecast model for '{crop}'", available=forecast_commodities()), 404
    return jsonify(crop=crop, unit_demand="quintals", unit_price="Rs/quintal",
                   forecast=[{"date": d["forecast_date"], "demand_qtl": d["predicted_demand_qty"],
                              "price_qtl": d["predicted_price_qtl"]} for d in ins["fc"]],
                   outlook=ins["outlook"], outlook_pct=ins["outlook_pct"])


@app.post("/api/routes/optimize-all")
@login_required("logistics", "admin")
def api_optimize_all():
    """Same contract as the team's FastAPI endpoint: optional {"depot_ids": [...]}."""
    body = request.get_json(silent=True) or {}
    res = planner.optimize(get_db(), body.get("depot_ids") or None)
    if not res["routes"] and not res["unassigned"]:
        return jsonify(status="nothing_to_plan",
                       detail="No region has both a free vehicle and packed orders waiting."), 400
    depots = []
    for p in res["plans"]:
        depots.append(dict(
            depot=dict(depot_id=p["depot"]["id"], name=p["depot"]["name"], lat=p["depot"]["lat"], lng=p["depot"]["lng"]),
            matrix_source=p["matrix_source"], unassigned_orders=p["unassigned"],
            routes=[dict(route_id=r["route_id"], vehicle=r["vehicle"]["code"], driver=r["vehicle"]["driver_name"],
                         total_distance_km=r["km"], estimated_time_mins=r["minutes"], baseline_km=r["naive_km"],
                         route_source=r["route_source"], geometry=r["geometry"],
                         stops=[{k: s[k] for k in ("seq", "kind", "order_id", "name", "lat", "lng", "qty", "eta_min")}
                                for s in r["stops"]]) for r in p["routes"]]))
    return jsonify(status="success", total_km=res["km"], baseline_km=res["naive_km"],
                   saved_km=res["saved_km"], saved_pct=res["saved_pct"], depots=depots,
                   unassigned_orders=res["unassigned"])


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True, use_reloader=False)
