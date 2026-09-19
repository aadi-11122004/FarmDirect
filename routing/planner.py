"""FarmDirect — multi-depot route planner.

Port of the team's FastAPI `/api/routes/optimize-all` logic (api/main.py) onto the
Flask + SQLite stack. Behaviour is the same:

  * routing is solved independently PER DEPOT / REGION — a depot's vehicles only
    see that depot's orders;
  * every order is a pickup (farm) -> delivery (buyer) pair, capacities are per
    vehicle, everything starts and ends at the depot;
  * distance/duration come from OSRM with an offline haversine fallback;
  * results are stored as routes + ordered stops, orders flip packed -> assigned.

Extras: a customer order is split into one SHIPMENT per farm (a pickup -> delivery
pair, exactly the unit the original solver models); orders that cannot be planned
in full are reported (with a reason) instead of failing the run; and every route
carries a "dispatched separately" baseline so the console can show km saved.
"""

import json
import os
from datetime import date, datetime

from routing.matrix import get_matrices, route_geometry
from routing.solver import solve_vrp_pd_multi

SERVICE_MIN = 6                                                # handling time per stop
MAX_SHIFT_MIN = int(os.getenv("FARMLINK_MAX_SHIFT_MIN", "600"))  # driver shift cap (10 h)
SOLVE_SECONDS = float(os.getenv("FARMLINK_SOLVE_SECONDS", "2"))
DEFAULT_CAPACITY_KG = 1000


def area_of(address):
    """'Flat 12, Shreeji Residency, Kothrud, Pune' -> 'Kothrud' (same idea as the sample app)."""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if len(parts) >= 3:
        return parts[-2][:24]
    return (parts[0] if parts else "Delivery")[:24]


# ------------------------------------------------------------------ DB reads
def load_depots(con):
    return [dict(r) for r in con.execute("SELECT * FROM depots ORDER BY id")]


def load_pool(con, depot_id):
    """Shipments (order x farm) that are packed and waiting for pickup in this region."""
    rows = con.execute(
        "SELECT o.id AS order_id, f.id AS farm_id, GROUP_CONCAT(oi.id) AS item_ids, "
        "SUM(oi.qty) AS qty, GROUP_CONCAT(oi.crop_name, ' + ') AS crop_name, "
        "o.address AS d_address, o.lat AS d_lat, o.lng AS d_lng, o.slot, "
        "f.name AS farm_name, f.village, f.lat AS p_lat, f.lng AS p_lng "
        "FROM order_items oi JOIN orders o ON o.id = oi.order_id "
        "JOIN listings l ON l.id = oi.listing_id JOIN farms f ON f.id = l.farm_id "
        "WHERE o.status = 'packed' AND o.depot_id = ? "
        "GROUP BY o.id, f.id ORDER BY o.id, f.id", (depot_id,))
    out = []
    for r in rows:
        d = dict(r)
        d["item_ids"] = [int(x) for x in d["item_ids"].split(",")]
        out.append(d)
    return out


def nearest_depot(depots, lat, lng):
    from routing.matrix import calculate_haversine
    return min(depots, key=lambda d: calculate_haversine(lat, lng, d["lat"], d["lng"]))


# ------------------------------------------------------------------ planning
def _merge_stops(stops):
    """Collapse consecutive same-order / same-kind / same-place stops (e.g. 3 items
    dropped at one door become one stop)."""
    merged = []
    for s in stops:
        prev = merged[-1] if merged else None
        if (prev and prev["kind"] == s["kind"] and prev["order_id"] == s["order_id"]
                and abs(prev["lat"] - s["lat"]) < 1e-6 and abs(prev["lng"] - s["lng"]) < 1e-6):
            prev["qty"] += s["qty"]
            prev["item_ids"] += s["item_ids"]
            prev["name"] = prev["name"] if s["name"] in prev["name"] else prev["name"] + " + " + s["name"]
        else:
            merged.append(dict(s))
    return merged


def plan_depot(depot, vehicles, items, time_limit_s=SOLVE_SECONDS,
               max_shift_min=MAX_SHIFT_MIN, service_min=SERVICE_MIN):
    """Solve one region. `vehicles` = free vehicles based here, `items` = its pool.

    Returns {"routes": [...], "unassigned": [{"order_id", "reason"}], "matrix_source",
             "km", "naive_km", "minutes"} — nothing is written to the DB.
    """
    result = {"depot": depot, "routes": [], "unassigned": [], "matrix_source": None,
              "km": 0.0, "naive_km": 0.0, "minutes": 0}
    if not items or not vehicles:
        if items and not vehicles:
            result["unassigned"] = [{"order_id": oid, "reason": "no free vehicle at this depot"}
                                    for oid in sorted({i["order_id"] for i in items})]
        return result

    caps = [int(v["capacity_kg"] or DEFAULT_CAPACITY_KG) for v in vehicles]
    max_cap = max(caps)

    # a shipment heavier than the biggest vehicle is split into vehicle-sized loads
    too_big = set()
    expanded = []
    for it in items:
        remaining = float(it["qty"])
        while remaining > 1e-6:
            part = min(remaining, float(max_cap))
            expanded.append(dict(it, qty=part))
            remaining -= part
    items = expanded

    depot_pt = {"lat": depot["lat"], "lng": depot["lng"]}
    all_items = list(items)
    dist = dur = source = None
    active, served, vroutes = list(all_items), set(), []
    dropped_orders = set()

    resolved = False
    for _attempt in range(5):
        n = len(active)
        waypoints = ([depot_pt]
                     + [{"lat": i["p_lat"], "lng": i["p_lng"]} for i in active]
                     + [{"lat": i["d_lat"], "lng": i["d_lng"]} for i in active])
        pairs = [(1 + k, 1 + n + k) for k in range(n)]
        demands = [0] + [int(round(i["qty"])) for i in active] + [-int(round(i["qty"])) for i in active]
        dist, dur, source = get_matrices(waypoints)
        vroutes = solve_vrp_pd_multi(
            dur, demands, pairs, caps, 0, time_limit_s=time_limit_s,
            service_seconds=service_min * 60,
            max_route_seconds=max_shift_min * 60 if max_shift_min else None) or []
        visited = {node for vr in vroutes for node in vr["route_indices"][1:-1]}
        done = {k for k in range(n) if (1 + k) in visited}
        # an order is planned all-or-nothing: if any of its farm shipments was dropped,
        # take the whole order out and solve again
        partial = {active[k]["order_id"] for k in range(n) if k not in done
                   and any(k2 in done for k2 in range(n) if active[k2]["order_id"] == active[k]["order_id"])}
        if not partial:
            served = {active[k]["order_id"] for k in done}
            resolved = True
            break
        dropped_orders |= partial
        active = [i for i in active if i["order_id"] not in partial]
        if not active:
            vroutes, served, resolved = [], set(), True
            break
    if not resolved:                      # could not converge -> plan nothing rather than half an order
        vroutes, served, active = [], set(), []
    result["matrix_source"] = source
    items = active
    n = len(items)

    for vr in vroutes:
        v = vehicles[vr["vehicle_id"]]
        idx = vr["route_indices"]
        raw, clock = [], 0.0
        for prev, node in zip(idx[:-1], idx[1:-1]):
            clock += dur[prev][node] / 60.0 + (service_min if prev != 0 else 0)
            k = (node - 1) if node <= n else (node - 1 - n)
            it = items[k]
            is_pickup = node <= n
            raw.append({
                "kind": "pickup" if is_pickup else "drop",
                "order_id": it["order_id"], "item_ids": list(it["item_ids"]),
                "name": (f"{it['crop_name']} · {it['farm_name']}" if is_pickup else area_of(it["d_address"])),
                "address": (f"{it['farm_name']}, {it['village']}" if is_pickup else it["d_address"]),
                "lat": it["p_lat"] if is_pickup else it["d_lat"],
                "lng": it["p_lng"] if is_pickup else it["d_lng"],
                "qty": float(it["qty"]), "slot": it["slot"], "eta_min": round(clock),
            })
        stops = _merge_stops(raw)
        for seq, s_ in enumerate(stops):
            s_["seq"] = seq

        km = sum(dist[a][b] for a, b in zip(idx[:-1], idx[1:])) / 1000.0
        travel_min = sum(dur[a][b] for a, b in zip(idx[:-1], idx[1:])) / 60.0
        minutes = round(travel_min + service_min * len(stops))

        order_ids = sorted({s_["order_id"] for s_ in stops})
        on_route = set(idx[1:-1])
        naive = 0.0
        for oid in order_ids:       # baseline: this route's share of each order goes out as its own round trip
            nodes = [1 + k for k, it in enumerate(items) if it["order_id"] == oid and (1 + k) in on_route]
            leg = [0] + nodes + [nodes[0] + n, 0]
            naive += sum(dist[a][b] for a, b in zip(leg[:-1], leg[1:])) / 1000.0

        full = [depot_pt] + stops + [depot_pt]
        geometry, route_source = route_geometry(full)

        result["routes"].append({
            "vehicle": v, "stops": stops, "km": round(km, 1), "naive_km": round(naive, 1),
            "minutes": minutes, "load_kg": round(sum(s_["qty"] for s_ in stops if s_["kind"] == "pickup"), 1),
            "geometry": geometry, "route_source": route_source, "order_ids": order_ids,
        })

    for oid in sorted({i["order_id"] for i in all_items} - served - too_big):
        result["unassigned"].append({"order_id": oid, "reason": "does not fit any vehicle within capacity / shift limits"})

    result["km"] = round(sum(r["km"] for r in result["routes"]), 1)
    result["naive_km"] = round(sum(r["naive_km"] for r in result["routes"]), 1)
    result["minutes"] = sum(r["minutes"] for r in result["routes"])
    return result


# ------------------------------------------------------------------ persistence
def save_plan(con, plan, rdate=None):
    """Write routes + stops and move orders packed -> assigned, vehicles -> on_route."""
    rdate = rdate or date.today().isoformat()
    depot = plan["depot"]
    for r in plan["routes"]:
        v = r["vehicle"]
        full = ([{"kind": "depot", "name": depot["name"], "lat": depot["lat"], "lng": depot["lng"]}]
                + [{k: s[k] for k in ("kind", "order_id", "name", "lat", "lng", "qty", "eta_min")} for s in r["stops"]]
                + [{"kind": "depot", "name": depot["name"], "lat": depot["lat"], "lng": depot["lng"]}])
        cur = con.execute(
            "INSERT INTO delivery_routes(depot_id,vehicle_id,rdate,total_km,naive_km,est_minutes,"
            "load_kg,matrix_source,route_source,route_stops,route_polyline,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (depot["id"], v["id"], rdate, r["km"], r["naive_km"], r["minutes"], r["load_kg"],
             plan["matrix_source"], r["route_source"], json.dumps(full), json.dumps(r["geometry"]),
             "planned", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        rid = cur.lastrowid
        r["route_id"] = rid
        for s in r["stops"]:
            con.execute(
                "INSERT INTO deliveries(route_id,order_id,vehicle_id,kind,stop_name,address,lat,lng,"
                "qty,slot,ddate,seq,eta_min,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending')",
                (rid, s["order_id"], v["id"], s["kind"], s["name"], s["address"], s["lat"], s["lng"],
                 s["qty"], s["slot"], rdate, s["seq"], s["eta_min"]))
        for oid in r["order_ids"]:
            con.execute("UPDATE orders SET status='assigned' WHERE id=? AND status='packed'", (oid,))
        con.execute("UPDATE vehicles SET status='on_route' WHERE id=?", (v["id"],))


def optimize(con, depot_ids=None, rdate=None):
    """Plan every (or the given) depot(s). Returns a summary dict for the UI."""
    depots = load_depots(con)
    if depot_ids:
        depots = [d for d in depots if d["id"] in depot_ids]
    plans, tot = [], {"routes": 0, "orders": 0, "km": 0.0, "naive_km": 0.0, "unassigned": []}
    for depot in depots:
        pool = load_pool(con, depot["id"])
        if not pool:
            continue
        vehicles = [dict(v) for v in con.execute(
            "SELECT * FROM vehicles WHERE depot_id=? AND status='idle' ORDER BY capacity_kg DESC, id",
            (depot["id"],))]
        plan = plan_depot(depot, vehicles, pool)
        save_plan(con, plan, rdate)
        plans.append(plan)
        tot["routes"] += len(plan["routes"])
        tot["orders"] += len({oid for r in plan["routes"] for oid in r["order_ids"]})
        tot["km"] += plan["km"]
        tot["naive_km"] += plan["naive_km"]
        tot["unassigned"] += [dict(u, depot=depot["name"]) for u in plan["unassigned"]]
    con.commit()
    tot["km"], tot["naive_km"] = round(tot["km"], 1), round(tot["naive_km"], 1)
    tot["saved_km"] = round(tot["naive_km"] - tot["km"], 1)
    tot["saved_pct"] = round(tot["saved_km"] / tot["naive_km"] * 100, 1) if tot["naive_km"] else 0.0
    tot["plans"] = plans
    return tot


def reset_plan(con):
    """Demo helper: undo every open (not completed) plan; orders go back to 'packed'."""
    routes = [r["id"] for r in con.execute("SELECT id FROM delivery_routes WHERE status!='completed'")]
    for rid in routes:
        for r in con.execute("SELECT DISTINCT order_id FROM deliveries WHERE route_id=?", (rid,)).fetchall():
            con.execute("UPDATE orders SET status='packed' WHERE id=? AND status IN ('assigned','in_transit')",
                        (r["order_id"],))
        con.execute("DELETE FROM deliveries WHERE route_id=?", (rid,))
        con.execute("DELETE FROM delivery_routes WHERE id=?", (rid,))
    con.execute("UPDATE vehicles SET status='idle'")
    con.commit()
    return len(routes)


# ------------------------------------------------------------------ mark-off flow
def mark_stop_done(con, stop_id):
    """Driver marks a stop done; cascades to order status, route and vehicle."""
    s = con.execute("SELECT * FROM deliveries WHERE id=?", (stop_id,)).fetchone()
    if not s or s["status"] == "done":
        return None
    con.execute("UPDATE deliveries SET status='done' WHERE id=?", (stop_id,))
    oid, rid = s["order_id"], s["route_id"]

    def left(kind):     # an order's shipments may ride on different vehicles -> count across routes
        return con.execute("SELECT COUNT(*) FROM deliveries WHERE order_id=? AND kind=? "
                           "AND status!='done'", (oid, kind)).fetchone()[0]

    if s["kind"] == "pickup" and left("pickup") == 0:
        con.execute("UPDATE orders SET status='in_transit' WHERE id=? AND status='assigned'", (oid,))
    if s["kind"] == "drop" and left("drop") == 0:
        con.execute("UPDATE orders SET status='delivered' WHERE id=?", (oid,))
    if con.execute("SELECT COUNT(*) FROM deliveries WHERE route_id=? AND status!='done'", (rid,)).fetchone()[0] == 0:
        con.execute("UPDATE delivery_routes SET status='completed' WHERE id=?", (rid,))
        con.execute("UPDATE vehicles SET status='idle' WHERE id=?", (s["vehicle_id"],))
    con.commit()
    return s


# ------------------------------------------------------------------ aggregation hook
def estimate_pickup_route(con, allocations, drop_lat, drop_lng, drop_address=""):
    """Route estimate for a Smart-Aggregation plan (farms -> buyer) using OR-Tools.

    Plugged into ml.matching.create_aggregation as `route_estimator`, replacing the
    hackathon placeholder. Returns None if it can't be solved (caller falls back).
    """
    depots = load_depots(con)
    if not depots or not allocations:
        return None
    depot = nearest_depot(depots, drop_lat, drop_lng)
    vehicles = [dict(v) for v in con.execute("SELECT * FROM vehicles WHERE depot_id=? ORDER BY id", (depot["id"],))]
    if not vehicles:
        return None
    caps = [int(v["capacity_kg"] or DEFAULT_CAPACITY_KG) for v in vehicles]

    max_cap = max(caps)
    loads = []                                   # (allocation, kg) — big allocations split into vehicle-sized loads
    for a in allocations:
        remaining = float(a["quantity_kg"])
        while remaining > 1e-6:
            part = min(remaining, float(max_cap))
            loads.append((a, part))
            remaining -= part
    n = len(loads)
    waypoints = ([{"lat": depot["lat"], "lng": depot["lng"]}]
                 + [{"lat": a["lat"], "lng": a["lng"]} for a, _ in loads]
                 + [{"lat": drop_lat, "lng": drop_lng}] * n)
    pairs = [(1 + k, 1 + n + k) for k in range(n)]
    demands = [0] + [int(round(kg)) for _, kg in loads] + [-int(round(kg)) for _, kg in loads]
    dist, dur, source = get_matrices(waypoints)
    vroutes = solve_vrp_pd_multi(dur, demands, pairs, caps, 0, time_limit_s=1.0,
                                 service_seconds=SERVICE_MIN * 60, max_route_seconds=None)
    if not vroutes:
        return None
    visited = {i for vr in vroutes for i in vr["route_indices"]}
    if any((1 + k) not in visited for k in range(n)):
        return None

    km = travel_min = 0.0
    stops = []
    for vr in vroutes:                                   # usually one truck; more if capacity binds
        idx = vr["route_indices"]
        km += sum(dist[a][b] for a, b in zip(idx[:-1], idx[1:])) / 1000.0
        travel_min += sum(dur[a][b] for a, b in zip(idx[:-1], idx[1:])) / 60.0
        for node in idx[1:-1]:
            if node <= n:
                a, kg = loads[node - 1]
                stops.append({"kind": "pickup", "name": a["farmer_name"], "lat": a["lat"], "lng": a["lng"],
                              "qty": kg})
            else:
                kg = loads[node - 1 - n][1]
                if stops and stops[-1]["kind"] == "drop":          # consecutive drops = one stop
                    stops[-1]["qty"] += kg
                else:
                    stops.append({"kind": "drop", "name": area_of(drop_address) or "Your location",
                                  "lat": drop_lat, "lng": drop_lng, "qty": kg})
    naive = sum((dist[0][1 + k] + dist[1 + k][1 + n + k] + dist[1 + n + k][0]) for k in range(n)) / 1000.0
    return {"km": round(km, 1), "naive_km": round(naive, 1), "vehicles": len(vroutes),
            "minutes": round(travel_min + SERVICE_MIN * len(stops)),
            "source": source, "depot": depot, "stops": stops}
