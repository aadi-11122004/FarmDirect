# Routing

**Unit of work:** a *shipment* = all items of one order coming from one farm → pickup node at the farm,
delivery node at the buyer. Shipments heavier than the biggest vehicle are split into vehicle-sized loads.

**Per depot:** free (`idle`) vehicles of that depot + packed orders tagged to that depot (buyer's nearest depot).

**Model** (`routing/solver.py`, OR-Tools): objective = total travel time; capacity dimension with `+kg` at
pickups and `-kg` at deliveries (simultaneous load ≤ vehicle capacity); pickup before its delivery on the same
vehicle; optional driver-shift dimension (travel + handling); every shipment optional at a huge penalty so
infeasible ones are reported instead of failing the plan. `planner.plan_depot` re-solves without any order that
was only partly planned (orders are all-or-nothing).

**Distances:** OSRM `table` + `route` when reachable; otherwise haversine at 30 km/h. A circuit breaker skips OSRM
for 2 minutes after a failure.

**Outputs:** `delivery_routes` (km, minutes, baseline km, load, polyline, stop snapshot) and `deliveries`
(ordered stops with ETA and status). Orders: `packed → assigned → in_transit` (all pickups done) `→ delivered`
(all drops done). Vehicles: `idle ↔ on_route`.

**Baseline:** each order (this route's share of it) sent out as its own depot→farm(s)→buyer→depot trip.

**Smart Sourcing** reuses the same solver (`planner.estimate_pickup_route`) to price the pickup route of an
aggregation plan at ₹12/km.
