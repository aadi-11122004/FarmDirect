"""FarmDirect — Supply Matching & Smart Aggregation engine.

Ported from the team's `Aggrigation Model/engine.py`. Scoring weights, filters
and the greedy allocation are unchanged. The pydantic request models became
plain dataclasses (the web app is Flask, not FastAPI), and — as the original
README promised — the placeholder logistics estimate can now be replaced by the
OR-Tools route optimizer via the optional `route_estimator` hook.
"""

from dataclasses import dataclass, field
from math import asin, cos, radians, sin, sqrt
from typing import Callable, List, Optional

GRADE_SCORE = {"A": 100, "B": 70, "C": 40}
LOGISTICS_RS_PER_KM = 12          # Rs per route-km (was: distance * 2 * 12 in the prototype)
PICKUP_PENALTY = 200              # Rs per extra farm stop


@dataclass
class Requirement:
    commodity: str
    quantity_kg: float
    delivery_lat: float
    delivery_lng: float
    required_date: str                       # YYYY-MM-DD
    quality_grade: str = "A"
    max_price_per_kg: Optional[float] = None
    max_distance_km: float = 100


@dataclass
class Listing:
    listing_id: str
    farmer_id: str
    farmer_name: str
    commodity: str
    available_quantity_kg: float
    price_per_kg: float
    quality_grade: str
    harvest_date: str
    latitude: float
    longitude: float
    harvest_type: str = "IMMEDIATE"          # IMMEDIATE or EXPECTED
    reliability_score: float = 80
    status: str = "ACTIVE"
    extra: dict = field(default_factory=dict)  # passthrough (village, is_fpo, ...)


def haversine_km(lat1, lon1, lat2, lon2):
    """Approximate distance between two latitude/longitude points."""
    R = 6371.0
    p1, p2 = radians(lat1), radians(lat2)
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(p1) * cos(p2) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(a))


def quality_ok(listing_grade, required_grade):
    return GRADE_SCORE.get(listing_grade, 0) >= GRADE_SCORE.get(required_grade, 0)


def price_score(price, all_prices):
    lo, hi = min(all_prices), max(all_prices)
    if hi == lo:
        return 100.0
    return round(100 * (hi - price) / (hi - lo), 2)


def distance_score(distance, max_distance):
    if max_distance <= 0:
        return 0
    return round(max(0, 100 * (1 - distance / max_distance)), 2)


def match_supply(requirement: Requirement, listings: List[Listing]):
    """Filter incompatible listings, score the rest, rank them."""
    eligible = []
    same = [x for x in listings
            if x.status == "ACTIVE"
            and x.commodity.lower() == requirement.commodity.lower()
            and x.available_quantity_kg > 0]
    all_prices = [x.price_per_kg for x in same] or [1]

    for x in same:
        if not quality_ok(x.quality_grade, requirement.quality_grade):
            continue
        if x.harvest_date > requirement.required_date:
            continue
        distance = haversine_km(x.latitude, x.longitude,
                                requirement.delivery_lat, requirement.delivery_lng)
        if distance > requirement.max_distance_km:
            continue
        if requirement.max_price_per_kg is not None and x.price_per_kg > requirement.max_price_per_kg:
            continue

        p_score = price_score(x.price_per_kg, all_prices)
        d_score = distance_score(distance, requirement.max_distance_km)
        q_score = GRADE_SCORE.get(x.quality_grade, 0)
        a_score = 100 if x.harvest_type.upper() == "IMMEDIATE" else 70
        r_score = x.reliability_score
        score = (0.30 * p_score + 0.25 * d_score + 0.20 * q_score
                 + 0.15 * a_score + 0.10 * r_score)

        eligible.append({
            "listing_id": x.listing_id, "farmer_id": x.farmer_id, "farmer_name": x.farmer_name,
            "commodity": x.commodity, "available_quantity_kg": x.available_quantity_kg,
            "price_per_kg": x.price_per_kg, "quality_grade": x.quality_grade,
            "harvest_type": x.harvest_type.upper(), "harvest_date": x.harvest_date,
            "distance_km": round(distance, 2), "reliability_score": x.reliability_score,
            "match_score": round(score, 2), "price_score": p_score, "distance_score": d_score,
            "quality_score": q_score, "availability_score": a_score,
            "lat": x.latitude, "lng": x.longitude, **x.extra,
        })

    eligible.sort(key=lambda c: c["match_score"], reverse=True)

    # Expected harvest is NOT treated as guaranteed; it is reported separately.
    confirmed = sum(c["available_quantity_kg"] for c in eligible if c["harvest_type"] == "IMMEDIATE")
    expected = sum(c["available_quantity_kg"] for c in eligible if c["harvest_type"] == "EXPECTED")
    return {
        "candidate_count": len(eligible),
        "confirmed_supply_kg": round(confirmed, 2),
        "expected_supply_kg": round(expected, 2),
        "total_potential_supply_kg": round(confirmed + expected, 2),
        "potential_shortage_kg": round(max(0, requirement.quantity_kg - confirmed - expected), 2),
        "candidates": eligible,
    }


def create_aggregation(requirement: Requirement, candidates: List[dict],
                       route_estimator: Optional[Callable[[List[dict]], Optional[dict]]] = None):
    """Greedy aggregation: best match score first, take only what is required.

    route_estimator(allocations) -> {"km", "minutes", "naive_km", "source", ...} lets the
    OR-Tools optimizer price the logistics. Without it (or if it fails) the original
    hackathon placeholder (2 x distance x Rs12) is used.
    """
    remaining = requirement.quantity_kg
    allocations = []
    for c in sorted(candidates, key=lambda x: x["match_score"], reverse=True):
        if remaining <= 0:
            break
        take = min(c["available_quantity_kg"], remaining)
        if take <= 0:
            continue
        allocations.append({
            "listing_id": c["listing_id"], "farmer_id": c["farmer_id"],
            "farmer_name": c["farmer_name"], "quantity_kg": round(take, 2),
            "price_per_kg": c["price_per_kg"], "produce_subtotal": round(take * c["price_per_kg"], 2),
            "distance_km": c["distance_km"], "harvest_type": c["harvest_type"],
            "quality_grade": c["quality_grade"], "match_score": c["match_score"],
            "lat": c["lat"], "lng": c["lng"],
            "village": c.get("village", ""),
        })
        remaining -= take

    allocated = requirement.quantity_kg - remaining
    produce_cost = sum(a["produce_subtotal"] for a in allocations)
    pickup_count = len(allocations)
    pickup_penalty = max(0, pickup_count - 1) * PICKUP_PENALTY

    route = None
    if route_estimator and allocations:
        try:
            route = route_estimator(allocations)
        except Exception as exc:                       # never break the page over an estimate
            print(f"[aggregation] route estimator failed, using placeholder: {exc}")
    if route:
        logistics = round(route["km"] * LOGISTICS_RS_PER_KM, 2)
        note = "Logistics priced from the OR-Tools optimised pickup route."
    else:
        logistics = round(sum(a["distance_km"] for a in allocations) * 2 * 12, 2)
        note = "Logistics is a straight-line placeholder (route optimizer unavailable)."

    return {
        "required_quantity_kg": requirement.quantity_kg,
        "allocated_quantity_kg": round(allocated, 2),
        "remaining_quantity_kg": round(max(0, remaining), 2),
        "fulfilled": remaining <= 0,
        "pickup_count": pickup_count,
        "produce_subtotal": round(produce_cost, 2),
        "estimated_logistics": logistics,
        "pickup_penalty": pickup_penalty,
        "estimated_total": round(produce_cost + logistics + pickup_penalty, 2),
        "allocations": allocations,
        "route": route,
        "note": note,
    }
