"""Write the two opening examples from real answers.

Each tool opens on a saved London example, so the first view makes no routing request.
Run this against a server that has a real Mapbox routing token and the Mapbox router:

    uvicorn server.app:app --port 8000          # in one terminal, with .env set
    python scripts/make_examples.py http://localhost:8000

It makes one Isochrone request and one solve (a few hundred Matrix elements), then writes
static/warp/example.json and static/meetup/example.json. Re-run it whenever the warp maths
or the response shapes change. --out-dir writes somewhere else (for trying things out).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.meetup.models import SolveResponse  # noqa: E402

LUT_SIZE = 2048

# The warp example: King's Cross, walking. Must match RING_TIMES and REF_ZOOM in
# static/warp/warpmath.js.
WARP = {
    "origin": {"lon": -0.1233, "lat": 51.5308, "label": "King's Cross"},
    "profile": "walking",
    "ref_zoom": 14,
    "travel_times": [5, 10, 15, 20],
}

# The meetup example: three friends at well-known places (not anybody's home), spread
# around the centre so the fairest place sits visibly between them.
MEETUP = {
    "objective": "minimax",
    "participants": [
        {"id": "1", "name": "Ana", "label": "Angel", "lon": -0.1058, "lat": 51.5322, "mode": "cycling"},
        {"id": "2", "name": "Ben", "label": "Vauxhall", "lon": -0.1253, "lat": 51.4861, "mode": "cycling"},
        {"id": "3", "name": "Cy", "label": "Marble Arch", "lon": -0.1589, "lat": 51.5131, "mode": "cycling"},
    ],
}


def check_warp(ex: dict) -> None:
    minutes = ex["contour_minutes"]
    assert 1 <= len(minutes) <= 4 and all(isinstance(m, int) and 1 <= m <= 60 for m in minutes), minutes
    rows = ex["source_radii"]
    assert len(rows) == len(minutes)
    for row in rows:
        assert len(row) == LUT_SIZE and all(math.isfinite(v) and v >= 0 for v in row)


def make_warp(client: httpx.Client) -> dict:
    o = WARP["origin"]
    resp = client.post("/api/warp-params", json={
        "longitude": o["lon"], "latitude": o["lat"], "zoom": WARP["ref_zoom"],
        "width": 1024, "height": 1024, "profile": WARP["profile"],
        "travel_times": WARP["travel_times"],
    })
    resp.raise_for_status()
    body = resp.json()
    ex = {
        "origin": o,
        "profile": WARP["profile"],
        "ref_zoom": WARP["ref_zoom"],
        "contour_minutes": [int(m) for m in body["contour_minutes"]],
        "source_radii": [[round(v, 1) for v in row] for row in body["source_radii"]],
    }
    check_warp(ex)
    return ex


def make_meetup(client: httpx.Client) -> dict:
    resp = client.post("/api/meetup/solve", json={
        "participants": [
            {"id": p["id"], "lon": p["lon"], "lat": p["lat"], "mode": p["mode"]}
            for p in MEETUP["participants"]
        ],
        "objective": MEETUP["objective"],
        "search": {"top_k": 4},
    })
    resp.raise_for_status()
    result = SolveResponse.model_validate(resp.json()).model_dump()
    result["meeting_area_geojson"] = None   # not drawn; keeps the file small
    return {**MEETUP, "result": result}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base_url")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "static")
    parser.add_argument("--only", choices=["warp", "meetup"], help="make just one of the two")
    args = parser.parse_args()

    made = {}
    with httpx.Client(base_url=args.base_url, timeout=120) as client:
        if args.only in (None, "warp"):
            made["warp"] = make_warp(client)
        if args.only in (None, "meetup"):
            made["meetup"] = make_meetup(client)

    for sub, data in made.items():
        path = args.out_dir / sub / "example.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, separators=(",", ":")) + "\n")
        print(f"wrote {path} ({path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
