"""FarmDirect — distance / duration matrices.

Live road-network distances from OSRM when reachable, otherwise the team's
offline haversine estimate (30 km/h straight-line, from solver/mock_matrix.py).

A small circuit-breaker keeps the app snappy on an offline demo laptop: after
one OSRM failure it is skipped for OSRM_COOLDOWN seconds instead of making every
click wait for a timeout. Set FARMLINK_OSRM=0 to force offline mode.
"""

import math
import os
import time

import requests

OSRM_URL = os.getenv("OSRM_URL", "http://router.project-osrm.org")
OSRM_ENABLED = os.getenv("FARMLINK_OSRM", "1") != "0"
OSRM_TIMEOUT = float(os.getenv("OSRM_TIMEOUT", "4"))
OSRM_COOLDOWN = 120
HEADERS = {"User-Agent": "FarmDirectApp/1.0"}
AVG_SPEED_MS = 8.33                 # ~30 km/h, used only by the offline fallback

_down_until = 0.0


def calculate_haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def generate_mock_matrices(locations):
    """Dummy N x N distance (m) and duration (s) matrices from straight-line distance."""
    n = len(locations)
    dist = [[0] * n for _ in range(n)]
    dur = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j:
                d = calculate_haversine(locations[i]["lat"], locations[i]["lng"],
                                        locations[j]["lat"], locations[j]["lng"])
                dist[i][j] = int(d)
                dur[i][j] = int(d / AVG_SPEED_MS)
    return dist, dur


def _osrm_ok():
    return OSRM_ENABLED and time.time() >= _down_until


def _trip_osrm():
    global _down_until
    _down_until = time.time() + OSRM_COOLDOWN


def get_matrices(waypoints):
    """-> (distance_m, duration_s, source) where source is 'osrm' or 'haversine'."""
    if _osrm_ok() and len(waypoints) > 1:
        try:
            coords = ";".join(f"{w['lng']},{w['lat']}" for w in waypoints)
            url = f"{OSRM_URL}/table/v1/driving/{coords}?annotations=distance,duration"
            resp = requests.get(url, headers=HEADERS, timeout=OSRM_TIMEOUT).json()
            if resp.get("code") != "Ok":
                raise RuntimeError(f"OSRM table code={resp.get('code')}")
            dist = [[int(round(v or 0)) for v in row] for row in resp["distances"]]
            dur = [[int(round(v or 0)) for v in row] for row in resp["durations"]]
            return dist, dur, "osrm"
        except Exception as exc:
            print(f"[routing] OSRM table unavailable ({exc}); using offline haversine estimate.")
            _trip_osrm()
    d, t = generate_mock_matrices(waypoints)
    return d, t, "haversine"


def route_geometry(ordered_stops):
    """Road polyline for an ordered stop list -> ([[lat, lng], ...], source).
    Falls back to straight legs between stops."""
    if _osrm_ok() and len(ordered_stops) > 1:
        try:
            coords = ";".join(f"{s['lng']},{s['lat']}" for s in ordered_stops)
            url = f"{OSRM_URL}/route/v1/driving/{coords}?overview=full&geometries=geojson"
            resp = requests.get(url, headers=HEADERS, timeout=OSRM_TIMEOUT).json()
            if resp.get("code") != "Ok":
                raise RuntimeError(f"OSRM route code={resp.get('code')}")
            return [[c[1], c[0]] for c in resp["routes"][0]["geometry"]["coordinates"]], "osrm"
        except Exception as exc:
            print(f"[routing] OSRM route unavailable ({exc}); drawing straight legs.")
            _trip_osrm()
    return [[s["lat"], s["lng"]] for s in ordered_stops], "straight_line"
