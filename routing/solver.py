"""FarmDirect — OR-Tools vehicle routing solver.

Ported from the team's `solver/vrp_solver.py` (multi-vehicle capacitated
pickup-and-delivery VRP). The model is unchanged; additions are opt-in keyword
arguments so the original call signature keeps working:

  time_limit_s        search budget (the prototype hard-coded 5 s; 1-3 s is plenty
                      for demo-sized regions and keeps the UI responsive)
  same_vehicle_groups lists of pickup nodes that must ride on the SAME vehicle
                      (all items of one customer order -> one delivery)
  service_seconds +   per-stop handling time and a driver-shift cap
  max_route_seconds   (a route can't be longer than one shift)
  drop_penalty        makes orders OPTIONAL at a huge cost, so an order that
                      cannot fit (capacity / shift) is reported as unassigned
                      instead of the whole plan failing with "infeasible"
"""

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

DROP_PENALTY = 10_000_000


def solve_vrp_pd_multi(distance_matrix, demands, pickup_deliveries, vehicle_capacities,
                       depot_index=0, *, time_limit_s=5.0, same_vehicle_groups=None,
                       service_seconds=0, max_route_seconds=None, drop_penalty=DROP_PENALTY):
    """Multi-vehicle Capacitated VRP with Pickup and Delivery.

    One vehicle per entry in vehicle_capacities; all start/end at depot_index.
    Returns a list of {"vehicle_id", "route_indices", "total_cost"} for vehicles
    that were given stops (idle vehicles omitted), or None if the solver finds
    nothing at all.
    """
    num_vehicles = len(vehicle_capacities)
    manager = pywrapcp.RoutingIndexManager(len(distance_matrix), num_vehicles, depot_index)
    routing = pywrapcp.RoutingModel(manager)

    def distance_callback(from_index, to_index):
        return distance_matrix[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]

    transit_cb = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_cb)

    def demand_callback(from_index):
        return demands[manager.IndexToNode(from_index)]

    demand_cb = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(demand_cb, 0, vehicle_capacities, True, "Capacity")

    if max_route_seconds:
        def time_callback(from_index, to_index):
            f, t = manager.IndexToNode(from_index), manager.IndexToNode(to_index)
            return distance_matrix[f][t] + (service_seconds if f != depot_index else 0)
        time_cb = routing.RegisterTransitCallback(time_callback)
        routing.AddDimension(time_cb, 0, int(max_route_seconds), True, "Time")

    solver = routing.solver()
    capacity_dim = routing.GetDimensionOrDie("Capacity")

    for pickup, delivery in pickup_deliveries:
        p_idx, d_idx = manager.NodeToIndex(pickup), manager.NodeToIndex(delivery)
        routing.AddPickupAndDelivery(p_idx, d_idx)
        solver.Add(routing.VehicleVar(p_idx) == routing.VehicleVar(d_idx))          # same vehicle
        solver.Add(capacity_dim.CumulVar(p_idx) <= capacity_dim.CumulVar(d_idx))     # pickup first
        if drop_penalty:
            routing.AddDisjunction([p_idx, d_idx], drop_penalty, 2)

    for group in (same_vehicle_groups or []):
        first = manager.NodeToIndex(group[0])
        for other in group[1:]:
            solver.Add(routing.VehicleVar(first) == routing.VehicleVar(manager.NodeToIndex(other)))

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromMilliseconds(int(time_limit_s * 1000))

    solution = routing.SolveWithParameters(params)
    if not solution:
        return None

    routes = []
    for vehicle_id in range(num_vehicles):
        index = routing.Start(vehicle_id)
        route, cost = [], 0
        while not routing.IsEnd(index):
            route.append(manager.IndexToNode(index))
            prev = index
            index = solution.Value(routing.NextVar(index))
            cost += routing.GetArcCostForVehicle(prev, index, vehicle_id)
        route.append(manager.IndexToNode(index))          # back to depot
        if len(route) > 2:                                # skip depot -> depot
            routes.append({"vehicle_id": vehicle_id, "route_indices": route, "total_cost": cost})
    return routes


def solve_cvrp_pd(distance_matrix, demands, pickup_deliveries, vehicle_capacity, depot_index=0):
    """Single-vehicle version, kept from the prototype for compatibility."""
    res = solve_vrp_pd_multi(distance_matrix, demands, pickup_deliveries, [vehicle_capacity],
                             depot_index, time_limit_s=2.0, drop_penalty=0)
    if not res:
        return None
    return {"route_indices": res[0]["route_indices"], "total_distance_meters": res[0]["total_cost"]}
