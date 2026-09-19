"""End-to-end smoke test: walks every role through the app with Flask's test client.
Run:  FARMLINK_OSRM=0 python tests/smoke_test.py   (needs a seeded + trained farmlink.db)"""
import os
import re
import sqlite3
import sys

os.environ.setdefault("FARMLINK_OSRM", "0")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from app import app  # noqa: E402

app.config["TESTING"] = True
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def client_for(email):
    c = app.test_client()
    r = c.post("/login", data={"email": email, "password": "demo123"})
    assert r.status_code == 302, f"login failed for {email}"
    return c


def db():
    con = sqlite3.connect(os.path.join(BASE, "farmlink.db"))
    con.row_factory = sqlite3.Row
    return con


# ---- public pages -------------------------------------------------------
anon = app.test_client()
for path in ["/", "/marketplace", "/marketplace?reg=all", "/product/1", "/login", "/register", "/api/forecast/Tomato"]:
    r = anon.get(path)
    check(f"GET {path}", r.status_code == 200, r.status_code)
check("forecast for un-modelled crop -> 404", anon.get("/api/forecast/Onion").status_code == 404)
check("landing shows AI chips", b"AI demand outlook" in anon.get("/").data)

# ---- consumer: cart -> checkout -> track ---------------------------------
c = client_for("consumer@fl.in")
mk = c.get("/marketplace")
ids = re.findall(rb"/product/(\d+)", mk.data)
check("consumer sees Pune listings", len(ids) > 3)
lid = int(ids[0])
r = c.post(f"/cart/add/{lid}", data={"qty": 3}, follow_redirects=True)
check("add to cart", b"Added" in r.data)
check("cart page", c.get("/cart").status_code == 200)
r = c.post("/checkout", data={"address": "Kothrud, Pune", "slot": "9–11 AM", "payment": "UPI (demo)"})
check("checkout redirects to success", r.status_code == 302, r.status_code)
oid = int(re.search(r"/order/(\d+)/success", r.headers["Location"]).group(1))
check("order success page", c.get(f"/order/{oid}/success").status_code == 200)
check("track (no route yet)", b"Not yet routed" in c.get(f"/track/{oid}").data)
con = db()
row = con.execute("SELECT depot_id, status FROM orders WHERE id=?", (oid,)).fetchone()
check("order tagged to Pune depot", row["depot_id"] == 1 and row["status"] == "placed")
check("consumer dashboard renders", c.get("/dashboard").status_code == 200)

# cross-region protection: a Pune consumer can't add a Nagpur farm's listing
nag = con.execute("SELECT l.id FROM listings l JOIN farms f ON f.id=l.farm_id WHERE f.depot_id=2 LIMIT 1").fetchone()["id"]
r = c.post(f"/cart/add/{nag}", data={"qty": 1}, follow_redirects=True)
check("cross-region add blocked", b"different region" in r.data)

# ---- farmer: confirm + pack the order; AI insights ----------------------
oi = con.execute("SELECT farmer_id FROM order_items WHERE order_id=?", (oid,)).fetchone()
fmail = con.execute("SELECT email FROM users WHERE id=?", (oi["farmer_id"],)).fetchone()["email"]
f = client_for(fmail)
check("farmer dashboard", f.get("/farmer").status_code == 200)
check("farmer listings", f.get("/farmer/listings").status_code == 200)
r = f.post("/farmer/listings/new", data={"crop_id": 1, "qty": 120, "price": 9, "grade": "A", "expected_date": ""})
check("new listing", r.status_code == 302)
for _ in range(2):
    f.post(f"/farmer/orders/{oid}/advance")
check("order packed by farmer", db().execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()["status"] == "packed")
check("insights (modelled crop)", b"14-day AI forecast" in client_for("ramesh@fl.in").get("/farmer/insights?crop=Tomato").data)
check("insights (crop without data)", b"no mandi data" in client_for("ramesh@fl.in").get("/farmer/insights?crop=Onion").data)

# ---- buyer: smart sourcing ----------------------------------------------
b = client_for("buyer@fl.in")
check("buyer dashboard", b.get("/buyer").status_code == 200)
r = b.get("/buyer/aggregate?crop=Tomato&qty=1200&grade=A&max_dist=150")
check("aggregate page renders plan", r.status_code == 200 and b"Farm-wise allocation" in r.data)
check("aggregate priced by OR-Tools (not placeholder)", b"One optimised round" in r.data and b"placeholder" not in r.data and b"<svg" in r.data)
r = b.post("/buyer/aggregate/order", data={"crop": "Tomato", "qty": 1200, "grade": "A", "by": "", "max_price": "", "max_dist": 150})
check("aggregated order placed", r.status_code == 302)
agg_oid = int(re.search(r"/order/(\d+)/success", r.headers["Location"]).group(1))
n_items = db().execute("SELECT COUNT(*) AS n FROM order_items WHERE order_id=?", (agg_oid,)).fetchone()["n"]
check("aggregated order has multi-farm items", n_items >= 2, n_items)

# the big aggregated order (850 kg from one farm > largest 800 kg vehicle) must still be routable
rf = client_for("ramesh@fl.in")
for _ in range(2):
    rf.post(f"/farmer/orders/{agg_oid}/advance")
check("aggregated order packed", db().execute("SELECT status FROM orders WHERE id=?", (agg_oid,)).fetchone()["status"] == "packed")

# ---- logistics: optimise, mark off, reset -------------------------------
L = client_for("logistics@fl.in")
r = L.get("/logistics")
check("console pool map", r.status_code == 200 and b"Packed orders awaiting pickup" in r.data and b"<svg" in r.data)
r = L.post("/logistics/optimize", data={"scope": "all", "d": 1}, follow_redirects=True)
check("optimize flashes result", b"OR-Tools planned" in r.data, r.data[:0])
con = db()
routes = con.execute("SELECT * FROM delivery_routes").fetchall()
check("routes saved", len(routes) >= 2, len(routes))
check("every route beats separate dispatch", all(x["total_km"] <= x["naive_km"] + 0.01 for x in routes))
cap_ok = True
for rt in routes:
    veh = con.execute("SELECT capacity_kg FROM vehicles WHERE id=?", (rt["vehicle_id"],)).fetchone()
    load = peak = 0
    for s in con.execute("SELECT kind, qty FROM deliveries WHERE route_id=? ORDER BY seq", (rt["id"],)):
        load += s["qty"] if s["kind"] == "pickup" else -s["qty"]
        peak = max(peak, load)
    cap_ok &= peak <= veh["capacity_kg"] + 1
check("capacity never exceeded", cap_ok)
seq_ok = True
for rt in routes:
    seen = set()
    for s_ in con.execute("SELECT * FROM deliveries WHERE route_id=? ORDER BY seq", (rt["id"],)):
        if s_["kind"] == "pickup":
            seen.add(s_["order_id"])
        elif s_["order_id"] not in seen:       # a drop needs an earlier pickup of the same order on this vehicle
            seq_ok = False
check("pickup always precedes its delivery on the same route", seq_ok)
still = con.execute("SELECT COUNT(*) AS n FROM orders WHERE status='packed'").fetchone()["n"]
flagged = len(re.findall(rb"left unassigned", r.data))
check("every packed order is routed or explicitly reported unassigned", still == flagged, f"{still} left vs {flagged} reported")
check("console shows route", b"Stops in order" in L.get("/logistics?d=1").data)
check("second depot console", b"Nagpur" in L.get("/logistics?d=2").data)
check("oversize aggregated order assigned (split into loads)", db().execute("SELECT status FROM orders WHERE id=?", (agg_oid,)).fetchone()["status"] == "assigned")
check("our order is now assigned", db().execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()["status"] == "assigned")
t = client_for("consumer@fl.in").get(f"/track/{oid}")
check("track shows vehicle + map", b"Delivery round" in t.data and b"<svg" in t.data and b"Driver" in t.data)

# mark every stop of our order's routes done, in route order
stops = db().execute("SELECT id, kind FROM deliveries WHERE order_id=? ORDER BY route_id, seq", (oid,)).fetchall()
for s in [x for x in stops if x["kind"] == "pickup"]:
    L.post(f"/logistics/stop/{s['id']}/done")
check("all pickups done -> in_transit", db().execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()["status"] == "in_transit")
for s in [x for x in stops if x["kind"] == "drop"]:
    L.post(f"/logistics/stop/{s['id']}/done")
check("all drops done -> delivered", db().execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()["status"] == "delivered")

r = L.post("/logistics/reset", follow_redirects=True)
check("reset plan", db().execute("SELECT COUNT(*) AS n FROM delivery_routes WHERE status!='completed'").fetchone()["n"] == 0)
r = L.post("/api/routes/optimize-all", json={})
check("API optimize-all", r.status_code == 200 and r.get_json()["status"] == "success", r.status_code)
check("API needs login", app.test_client().post("/api/routes/optimize-all", json={}).status_code == 302)

# ---- admin ---------------------------------------------------------------
A = client_for("admin@fl.in")
r = A.get("/admin")
check("admin dashboard", r.status_code == 200 and b"saved vs separate dispatch" in r.data)
check("admin sees logistics", A.get("/logistics").status_code == 200)
check("consumer blocked from /admin", client_for("consumer@fl.in").get("/admin").status_code == 302)

# ---- Nagpur users -------------------------------------------------------
N = client_for("ocmart@fl.in")
check("Nagpur buyer sourcing", b"Farm-wise allocation" in N.get("/buyer/aggregate?crop=Nagpur+Orange&qty=300&grade=A&max_dist=150").data)
D = client_for("deepak@fl.in")
check("Nagpur farmer dashboard", D.get("/farmer").status_code == 200)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
