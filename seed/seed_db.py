"""FarmDirect — database schema + demo data seeder (Pune & Nagpur regions).

Creates farmlink.db with users (farmers/FPOs/consumers/bulk buyers/logistics/admin),
depots, farms, listings, orders, vehicles (with drivers), routes, stops, quotes and
the two ML tables (market_price_historical, demand_forecast_logs).
All demo accounts share the password: demo123

Mandi reference prices for crops that appear in ml/data/*.csv (Tomato, Wheat) are
taken from the real Agmarknet data; other crops use sensible fallbacks until you add
their CSVs and retrain.
"""

import os
import random
import sqlite3
import sys
from datetime import date, timedelta

from werkzeug.security import generate_password_hash

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

DB = os.path.join(BASE, "farmlink.db")
random.seed(7)
PW = generate_password_hash("demo123")

SCHEMA = """
CREATE TABLE depots(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, lat REAL NOT NULL, lng REAL NOT NULL);
CREATE TABLE users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN
    ('farmer','consumer','buyer','logistics','admin')),
  phone TEXT, address TEXT, lat REAL, lng REAL, depot_id INTEGER REFERENCES depots(id),
  created_at TEXT DEFAULT (date('now')));
CREATE TABLE farms(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER REFERENCES users(id),
  name TEXT NOT NULL, fpo_name TEXT, is_fpo INTEGER DEFAULT 0,
  village TEXT, district TEXT, lat REAL, lng REAL, depot_id INTEGER REFERENCES depots(id),
  organic INTEGER DEFAULT 0, area_acres REAL, rating REAL DEFAULT 4.5,
  reliability REAL DEFAULT 85);
CREATE TABLE crops(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, emoji TEXT,
  category TEXT, unit TEXT DEFAULT 'kg', base_price REAL);
CREATE TABLE listings(
  id INTEGER PRIMARY KEY AUTOINCREMENT, farm_id INTEGER REFERENCES farms(id),
  crop_id INTEGER REFERENCES crops(id), qty_available REAL, price REAL,
  market_price REAL, unit TEXT DEFAULT 'kg', grade TEXT DEFAULT 'A',
  organic INTEGER DEFAULT 0, min_order REAL DEFAULT 1,
  harvest_type TEXT DEFAULT 'IMMEDIATE', harvest_date TEXT,
  status TEXT DEFAULT 'active', rating REAL DEFAULT 4.6, image_path TEXT);
CREATE TABLE orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, buyer_id INTEGER REFERENCES users(id),
  buyer_type TEXT DEFAULT 'retail', status TEXT DEFAULT 'placed',
  subtotal REAL, savings REAL, delivery_fee REAL, total REAL,
  payment_mode TEXT DEFAULT 'UPI (demo)', address TEXT, lat REAL, lng REAL,
  depot_id INTEGER REFERENCES depots(id),
  slot TEXT, placed_on TEXT DEFAULT (date('now')),
  created_ts TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE order_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER REFERENCES orders(id),
  listing_id INTEGER REFERENCES listings(id), farmer_id INTEGER,
  crop_id INTEGER, crop_name TEXT, qty REAL, price REAL, amount REAL);
CREATE TABLE vehicles(
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, vtype TEXT, capacity_kg REAL,
  driver_name TEXT, phone TEXT, depot_id INTEGER REFERENCES depots(id), status TEXT DEFAULT 'idle');
CREATE TABLE delivery_routes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, depot_id INTEGER REFERENCES depots(id),
  vehicle_id INTEGER REFERENCES vehicles(id), rdate TEXT, total_km REAL, naive_km REAL,
  est_minutes INTEGER, load_kg REAL, matrix_source TEXT, route_source TEXT,
  route_stops TEXT, route_polyline TEXT, status TEXT DEFAULT 'planned', created_at TEXT);
CREATE TABLE deliveries(
  id INTEGER PRIMARY KEY AUTOINCREMENT, route_id INTEGER REFERENCES delivery_routes(id),
  order_id INTEGER REFERENCES orders(id), vehicle_id INTEGER REFERENCES vehicles(id),
  kind TEXT CHECK(kind IN ('pickup','drop')), stop_name TEXT, address TEXT,
  lat REAL, lng REAL, qty REAL, slot TEXT, ddate TEXT, seq INTEGER DEFAULT 0,
  eta_min INTEGER, status TEXT DEFAULT 'pending');
CREATE TABLE quotes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, buyer_name TEXT, org TEXT, crop_name TEXT,
  qty_kg REAL, contact TEXT, note TEXT, status TEXT DEFAULT 'open',
  created_at TEXT DEFAULT (date('now')));
CREATE TABLE market_price_historical(
  id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_date TEXT, category TEXT, commodity TEXT,
  crop_name TEXT, mandi_name TEXT, modal_price REAL, arrival_volume REAL);
CREATE TABLE demand_forecast_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, commodity TEXT, forecast_date TEXT,
  predicted_demand_qty REAL, predicted_price_qtl REAL, demand_trend_label TEXT,
  generated_at TEXT);
CREATE INDEX idx_orders_depot ON orders(depot_id, status);
CREATE INDEX idx_deliv_route ON deliveries(route_id, seq);
CREATE INDEX idx_mph ON market_price_historical(commodity, recorded_date);
"""

# name, emoji, category, unit, fallback mandi price Rs/kg (Tomato & Wheat come from the CSV)
CROP_SPEC = [
    ("Tomato", "🍅", "Vegetable", "kg", 18.0),
    ("Onion", "🧅", "Vegetable", "kg", 24.0),
    ("Potato", "🥔", "Vegetable", "kg", 26.0),
    ("Carrot", "🥕", "Vegetable", "kg", 40.0),
    ("Spinach", "🥬", "Leafy Greens", "kg", 30.0),
    ("Wheat", "🌾", "Grain", "kg", 26.0),
    ("Banana", "🍌", "Fruit", "kg", 40.0),
    ("Nagpur Orange", "🍊", "Fruit", "kg", 60.0),
]

DEPOTS = [
    ("Pune Central Depot", 18.5018, 73.8636),
    ("Nagpur Central Depot", 21.1458, 79.0882),
]

# name, email, role, phone, address, lat, lng, depot index (0 Pune / 1 Nagpur)
USERS = [
    # ---- Pune farmers
    ("Ramesh Patil", "ramesh@fl.in", "farmer", "98220 11223", "Patil Shetkari Farms, Baramati", 18.1514, 74.5771, 0),
    ("Sunita Shinde", "sunita@fl.in", "farmer", "98220 44556", "Shinde Organic Farm, Junnar", 19.2100, 73.8700, 0),
    ("Vijay Jadhav", "vijay@fl.in", "farmer", "98220 77889", "Jadhav Farms, Narayangaon", 19.2830, 73.8830, 0),
    ("Sahyadri FPO (Mangal Pawar)", "fpo@fl.in", "farmer", "98220 99001", "Sahyadri FPO Collection Centre, Mulshi", 18.5100, 73.6400, 0),
    ("Mangal Pawar", "mangal@fl.in", "farmer", "98220 33445", "Pawar Mala, Saswad", 18.3450, 73.8500, 0),
    # ---- Nagpur farmers
    ("Deepak Wagh", "deepak@fl.in", "farmer", "98230 11223", "Wagh Orange Orchards, Kalmeshwar", 21.2277, 78.9106, 1),
    ("Katol Citrus FPO (Sunil Raut)", "katolfpo@fl.in", "farmer", "98230 44556", "Katol Citrus Growers FPO, Katol", 21.2647, 78.9629, 1),
    # ---- Pune consumers
    ("Ananya Kulkarni", "consumer@fl.in", "consumer", "90280 11223", "Flat 12, Shreeji Residency, Kothrud, Pune", 18.5074, 73.8077, 0),
    ("Priya Deshmukh", "priya@fl.in", "consumer", "90280 33445", "Nagar Road, Viman Nagar, Pune", 18.5679, 73.9143, 0),
    ("Rohan Kadam", "rohan@fl.in", "consumer", "90280 55667", "Kharadi Bypass Rd, Kharadi, Pune", 18.5510, 73.9420, 0),
    ("Amit Joshi", "amit@fl.in", "consumer", "90280 77889", "Baner Road, Baner, Pune", 18.5590, 73.7868, 0),
    ("Neha Verma", "neha@fl.in", "consumer", "90280 99001", "Phase 2, Hinjewadi, Pune", 18.5913, 73.7389, 0),
    ("Kiran Bhosale", "kiran@fl.in", "consumer", "90280 22334", "Wagholi, Pune", 18.5814, 73.9523, 0),
    ("Meera Rane", "meera@fl.in", "consumer", "90280 44556", "Karve Nagar, Pune", 18.4890, 73.8220, 0),
    # ---- Nagpur consumers
    ("Sanjay Deshpande", "sanjay@fl.in", "consumer", "90290 11223", "Ravi Nagar Road, Dharampeth, Nagpur", 21.1394, 79.0578, 1),
    ("Pooja Nair", "pooja@fl.in", "consumer", "90290 33445", "Manish Nagar, Nagpur", 21.0980, 79.0570, 1),
    ("Rahul Meshram", "rahul@fl.in", "consumer", "90290 55667", "Civil Lines, Sadar, Nagpur", 21.1600, 79.0800, 1),
    ("Kavita Bhoyar", "kavita@fl.in", "consumer", "90290 77889", "Ring Road, Wardhaman Nagar, Nagpur", 21.1300, 79.1200, 1),
    # ---- bulk buyers
    ("Chef Vikram Khanna", "buyer@fl.in", "buyer", "91110 11223", "Hotel Green Leaf, Koregaon Park, Pune", 18.5362, 73.8939, 0),
    ("Daily Basket Retail Pvt Ltd", "dailybasket@fl.in", "buyer", "91110 33445", "Daily Basket Store, Aundh, Pune", 18.5636, 73.8077, 0),
    ("Annapurna Mess & Caterers", "annapurna@fl.in", "buyer", "91110 55667", "Annapurna Mess, Swargate, Pune", 18.5010, 73.8600, 0),
    ("Orange City Mart", "ocmart@fl.in", "buyer", "91120 11223", "Orange City Mart, Sitabuldi, Nagpur", 21.1498, 79.0882, 1),
    # ---- staff
    ("Sunil Gaikwad (Dispatch)", "logistics@fl.in", "logistics", "90000 11223", "FarmDirect Depot, Pune", 18.5018, 73.8636, 0),
    ("Platform Admin", "admin@fl.in", "admin", "90000 00000", "FarmDirect HQ, Pune", 18.5204, 73.8567, 0),
]

# user_email, name, fpo_name, is_fpo, village, district, lat, lng, depot idx, organic, acres, rating, reliability
FARMS = [
    ("ramesh@fl.in", "Patil Shetkari Farms", None, 0, "Baramati", "Pune", 18.1514, 74.5771, 0, 0, 12.5, 4.7, 92),
    ("sunita@fl.in", "Shinde Organic Farm", None, 0, "Junnar", "Pune", 19.2100, 73.8700, 0, 1, 6.0, 4.9, 95),
    ("vijay@fl.in", "Jadhav Farms", None, 0, "Narayangaon", "Pune", 19.2830, 73.8830, 0, 0, 9.0, 4.6, 88),
    ("fpo@fl.in", "Sahyadri Farmer Producer Co.", "Sahyadri FPO", 1, "Mulshi", "Pune", 18.5100, 73.6400, 0, 0, 140.0, 4.8, 94),
    ("mangal@fl.in", "Pawar Mala", None, 0, "Saswad", "Pune", 18.3450, 73.8500, 0, 0, 4.5, 4.5, 84),
    (None, "MoreMala Agro", None, 0, "Indapur", "Pune", 18.1130, 74.9300, 0, 0, 15.0, 4.4, 82),
    (None, "Bhor Valley Collective", "Bhor FPO", 1, "Bhor", "Pune", 18.0450, 73.8450, 0, 1, 55.0, 4.8, 93),
    (None, "Kamshet Greens", None, 0, "Kamshet", "Pune", 18.7600, 73.5450, 0, 0, 7.5, 4.5, 86),
    ("deepak@fl.in", "Wagh Orange Orchards", None, 0, "Kalmeshwar", "Nagpur", 21.2277, 78.9106, 1, 0, 22.0, 4.8, 93),
    ("katolfpo@fl.in", "Katol Citrus Growers FPO", "Katol Citrus FPO", 1, "Katol", "Nagpur", 21.2647, 78.9629, 1, 0, 180.0, 4.7, 91),
    (None, "Hingna Vegetable Farm", None, 0, "Hingna", "Nagpur", 21.1120, 78.9810, 1, 0, 8.0, 4.5, 87),
    (None, "Kamptee Agro", None, 0, "Kamptee", "Nagpur", 21.2250, 79.1950, 1, 0, 30.0, 4.4, 83),
]

# farm name -> [(crop, qty_kg, grade, min_order, harvested_days_ago, harvest_type)]
FARM_CROPS = {
    "Patil Shetkari Farms": [("Tomato", 850, "A", 1, 1, "IMMEDIATE"), ("Onion", 2400, "A", 5, 3, "IMMEDIATE"),
                             ("Wheat", 1800, "A", 25, 9, "IMMEDIATE")],
    "Shinde Organic Farm": [("Spinach", 180, "A", 1, 0, "IMMEDIATE"), ("Carrot", 260, "A", 1, 1, "IMMEDIATE"),
                            ("Tomato", 340, "A", 1, 1, "IMMEDIATE")],
    "Jadhav Farms": [("Tomato", 520, "B", 1, 1, "IMMEDIATE"), ("Onion", 1100, "B", 5, 4, "IMMEDIATE"),
                     ("Potato", 600, "A", 5, 3, "IMMEDIATE")],
    "Sahyadri Farmer Producer Co.": [("Wheat", 2800, "A", 25, 12, "IMMEDIATE"), ("Onion", 1800, "A", 25, 5, "IMMEDIATE"),
                                     ("Tomato", 400, "A", 5, 1, "IMMEDIATE")],
    "Pawar Mala": [("Banana", 780, "A", 2, 1, "IMMEDIATE"), ("Tomato", 260, "B", 1, 2, "IMMEDIATE")],
    "MoreMala Agro": [("Potato", 1900, "A", 5, 4, "IMMEDIATE"), ("Onion", 1500, "B", 5, 6, "IMMEDIATE"),
                      ("Tomato", 300, "A", 5, -2, "EXPECTED")],
    "Bhor Valley Collective": [("Spinach", 220, "A", 1, 0, "IMMEDIATE"), ("Carrot", 300, "A", 1, 1, "IMMEDIATE"),
                               ("Tomato", 250, "A", 1, 1, "IMMEDIATE")],
    "Kamshet Greens": [("Banana", 780, "A", 2, 1, "IMMEDIATE"), ("Tomato", 350, "A", 2, -3, "EXPECTED")],
    "Wagh Orange Orchards": [("Nagpur Orange", 900, "A", 2, 2, "IMMEDIATE"), ("Tomato", 300, "A", 1, 1, "IMMEDIATE")],
    "Katol Citrus Growers FPO": [("Nagpur Orange", 2000, "A", 25, 3, "IMMEDIATE"), ("Wheat", 1500, "A", 25, 10, "IMMEDIATE")],
    "Hingna Vegetable Farm": [("Tomato", 600, "A", 1, 1, "IMMEDIATE"), ("Onion", 800, "B", 5, 4, "IMMEDIATE"),
                              ("Spinach", 120, "A", 1, 0, "IMMEDIATE")],
    "Kamptee Agro": [("Wheat", 1200, "B", 10, 8, "IMMEDIATE"), ("Potato", 700, "A", 5, 3, "IMMEDIATE")],
}

# name, type, capacity kg, driver, phone, depot idx
VEHICLES = [
    ("MH-12-AB-1234", "Tata Ace (Tempo)", 800, "Suresh Kumar", "98765 00003", 0),
    ("MH-12-CD-5678", "Mahindra Jeeto", 500, "Anita Deshmukh", "98765 00005", 0),
    ("MH-12-EF-9012", "E-loader (Piaggio)", 300, "Rajesh Pawar", "98765 00009", 0),
    ("MH-31-XY-1111", "Tempo", 500, "Vikram Rao", "98765 00005", 1),
    ("MH-31-XY-2222", "Mini Truck", 400, "Meera Joshi", "98765 00006", 1),
]

SLOTS = ["9–11 AM", "11 AM–1 PM", "4–7 PM"]


def market_reference_prices():
    """Latest mandi price (Rs/kg) per crop from the Agmarknet CSVs, where available."""
    try:
        from ml.data_processor import latest_mandi_price_per_kg, load_all
        df = load_all()
    except Exception as exc:                                 # pandas missing / no CSV
        print(f"[seed] no mandi CSV data ({exc}); using fallback reference prices")
        return {}
    out = {}
    for name, *_ in CROP_SPEC:
        p = latest_mandi_price_per_kg(df, name)
        if p:
            out[name] = p
    return out


def main():
    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    ref = market_reference_prices()

    depot_ids = [con.execute("INSERT INTO depots(name,lat,lng) VALUES(?,?,?)", d).lastrowid for d in DEPOTS]

    crop_ids, base_by_name = {}, {}
    for name, emoji, cat, unit, fallback in CROP_SPEC:
        base = ref.get(name, fallback)
        base_by_name[name] = base
        crop_ids[name] = con.execute(
            "INSERT INTO crops(name,emoji,category,unit,base_price) VALUES(?,?,?,?,?)",
            (name, emoji, cat, unit, base)).lastrowid
    print("[seed] mandi reference prices Rs/kg:", base_by_name,
          "(from CSV: " + ", ".join(ref) + ")" if ref else "")

    user_ids, user_by_email = {}, {}
    for name, email, role, phone, addr, lat, lng, di in USERS:
        uid = con.execute(
            "INSERT INTO users(name,email,password_hash,role,phone,address,lat,lng,depot_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (name, email, PW, role, phone, addr, lat, lng, depot_ids[di])).lastrowid
        user_ids[email] = uid
        user_by_email[email] = dict(id=uid, name=name, email=email, role=role, address=addr,
                                    lat=lat, lng=lng, depot=depot_ids[di])

    today = date.today()
    farm_ids, listing_pool = {}, {0: [], 1: []}
    for email, name, fpo, isfpo, village, district, lat, lng, di, organic, acres, rating, rel in FARMS:
        fid = con.execute(
            "INSERT INTO farms(user_id,name,fpo_name,is_fpo,village,district,lat,lng,depot_id,"
            "organic,area_acres,rating,reliability) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user_ids.get(email), name, fpo, isfpo, village, district, lat, lng, depot_ids[di],
             organic, acres, rating, rel)).lastrowid
        farm_ids[name] = fid
        for crop, qty, grade, min_order, hdays, htype in FARM_CROPS[name]:
            base = base_by_name[crop]
            price = round(base * random.uniform(0.76, 0.83) * (1.12 if organic else 1.0), 1)
            hdate = (today - timedelta(days=hdays)).isoformat() if htype == "IMMEDIATE" \
                else (today + timedelta(days=-hdays)).isoformat()
            lid = con.execute(
                "INSERT INTO listings(farm_id,crop_id,qty_available,price,market_price,unit,grade,organic,"
                "min_order,harvest_type,harvest_date,status,rating) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fid, crop_ids[crop], qty, price, base, "kg", grade, organic, min_order, htype, hdate,
                 "active", round(random.uniform(4.3, 4.9), 1))).lastrowid
            if htype == "IMMEDIATE":                         # only ready stock is orderable
                listing_pool[di].append(dict(id=lid, farm=name, crop=crop, price=price, market=base,
                                             farmer_uid=user_ids.get(email), crop_id=crop_ids[crop],
                                             farm_id=fid))

    def make_order(buyer, status, when, pool, items_n=None, ts=None):
        bulk = buyer["role"] == "buyer"
        farms_used, items = set(), []
        for it in random.sample(pool, len(pool)):
            if it["farm_id"] in farms_used:
                continue
            farms_used.add(it["farm_id"])
            items.append(it)
            if len(items) >= (items_n or random.randint(1, 3)):
                break
        subtotal = savings = 0.0
        rows = []
        for it in items:
            qty = random.randint(30, 110) if bulk else random.randint(1, 6)
            amt = round(qty * it["price"], 2)
            subtotal += amt
            savings += round((it["market"] - it["price"]) * qty, 2)
            rows.append((it, qty, amt))
        fee = 0 if subtotal >= 499 else 29
        oid = con.execute(
            "INSERT INTO orders(buyer_id,buyer_type,status,subtotal,savings,delivery_fee,total,payment_mode,"
            "address,lat,lng,depot_id,slot,placed_on,created_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (buyer["id"], "bulk" if bulk else "retail", status, round(subtotal, 2), round(savings, 2), fee,
             round(subtotal + fee, 2), random.choice(["UPI (demo)", "UPI (demo)", "Cash on delivery"]),
             buyer["address"], buyer["lat"], buyer["lng"], buyer["depot"], random.choice(SLOTS),
             when.isoformat(), ts or f"{when} {random.randint(8, 20):02d}:{random.randint(0, 59):02d}:00")).lastrowid
        for it, qty, amt in rows:
            con.execute(
                "INSERT INTO order_items(order_id,listing_id,farmer_id,crop_id,crop_name,qty,price,amount) "
                "VALUES(?,?,?,?,?,?,?,?)", (oid, it["id"], it["farmer_uid"], it["crop_id"], it["crop"], qty,
                                             it["price"], amt))
        return oid

    # ---- history: last 30 days delivered orders, both regions
    consumers = [u for u in user_by_email.values() if u["role"] in ("consumer", "buyer")]
    for days_back in range(30, 0, -1):
        d = today - timedelta(days=days_back)
        for _ in range(random.randint(4, 8)):
            buyer = random.choice(consumers)
            make_order(buyer, "delivered", d, listing_pool[depot_ids.index(buyer["depot"])])

    # ---- today's live orders (demo flow: placed -> confirmed -> packed -> [optimize] -> assigned ...)
    live = [
        # Pune
        ("placed", "priya@fl.in"), ("placed", "amit@fl.in"), ("confirmed", "rohan@fl.in"),
        ("confirmed", "consumer@fl.in"), ("packed", "neha@fl.in"), ("packed", "kiran@fl.in"),
        ("packed", "meera@fl.in"), ("packed", "buyer@fl.in"), ("packed", "dailybasket@fl.in"),
        ("packed", "annapurna@fl.in"), ("packed", "priya@fl.in"), ("packed", "consumer@fl.in"),
        # Nagpur
        ("placed", "sanjay@fl.in"), ("confirmed", "pooja@fl.in"), ("packed", "rahul@fl.in"),
        ("packed", "kavita@fl.in"), ("packed", "ocmart@fl.in"), ("packed", "sanjay@fl.in"),
    ]
    for status, email in live:
        b = user_by_email[email]
        make_order(b, status, today, listing_pool[depot_ids.index(b["depot"])],
                   ts=f"{today} {random.randint(7, 10):02d}:{random.randint(0, 59):02d}:00")

    # ---- fleet
    for code, vtype, cap, driver, phone, di in VEHICLES:
        con.execute("INSERT INTO vehicles(code,vtype,capacity_kg,driver_name,phone,depot_id,status) "
                    "VALUES(?,?,?,?,?,?,'idle')", (code, vtype, cap, driver, phone, depot_ids[di]))

    # ---- bulk quotes
    con.execute("INSERT INTO quotes(buyer_name,org,crop_name,qty_kg,contact,note,status) VALUES(?,?,?,?,?,?,?)",
                ("Chef Vikram Khanna", "Hotel Green Leaf", "Tomato", 500, "buyer@fl.in",
                 "Weekly contract, Grade A only", "open"))
    con.execute("INSERT INTO quotes(buyer_name,org,crop_name,qty_kg,contact,note,status) VALUES(?,?,?,?,?,?,?)",
                ("Annapurna Mess & Caterers", "Annapurna", "Onion", 300, "annapurna@fl.in",
                 "Monthly supply", "open"))

    con.commit()
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("users", "depots", "farms", "crops", "listings", "orders", "order_items",
                        "vehicles", "quotes")}
    con.close()
    print(f"[seed] farmlink.db ready: {counts}")
    print("[seed] demo logins (password: demo123): ramesh@fl.in (farmer), fpo@fl.in (FPO), deepak@fl.in "
          "(Nagpur farmer), consumer@fl.in, buyer@fl.in, ocmart@fl.in (Nagpur buyer), "
          "logistics@fl.in, admin@fl.in")


if __name__ == "__main__":
    main()
