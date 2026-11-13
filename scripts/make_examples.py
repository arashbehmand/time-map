"""Write the two opening examples from real answers.

Each tool opens on a saved London example, so the first view makes no routing request.
Run this against a server that has a real Mapbox routing token and the Mapbox router:

    uvicorn server.app:app --port 8000          # in one terminal, with .env set
    python scripts/make_examples.py http://localhost:8000

It makes two Isochrone requests (the walking field) and one solve (a few hundred Matrix
elements), then writes static/warp/example-field.json and static/meetup/example.json. Re-run
it whenever the field or the response shapes change. --out-dir writes somewhere else.

--from-saved FILE writes the warp example without any request, from the radial example the
old warp page used (static/warp/example.json in git history before the radial table was
retired): a real Mapbox walking answer for King's Cross, 5/10/15/20 minutes at Mapbox's
5.1 km/h, relabelled to the page's 4 km/h. It lacks the islands and holes a live answer has,
so regenerate from a server when a token is at hand.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.meetup.models import SolveResponse  # noqa: E402

# The warp example: King's Cross, walking.
WARP = {"origin": {"lon": -0.1233, "lat": 51.5308, "label": "King's Cross"}, "mode": "walking"}
MAPBOX_WALK_KMH = 5.1           # Mapbox's default walking speed
PAGE_WALK_KMH = 4.0             # the page's (and the London tables') walking speed

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
    from server.warp.field import decode
    assert ex["mode"] == "walking" and ex["origin"]["label"]
    minutes, _ = decode(ex["minutes_b64"], (ex["ny"], ex["nx"]))
    j, i = round(-ex["y0"] / ex["cell"]), round(-ex["x0"] / ex["cell"])
    assert minutes[j, i] == 0, "the pin is not at 0 minutes"
    assert np.nanmax(minutes[np.isfinite(minutes)]) > 10


def make_warp(client: httpx.Client) -> dict:
    o = WARP["origin"]
    resp = client.post("/api/travel-time-field", json={"longitude": o["lon"], "latitude": o["lat"], "mode": WARP["mode"]})
    resp.raise_for_status()
    ex = resp.json()
    ex["origin"] = {**ex["origin"], "label": o["label"]}
    check_warp(ex)
    return ex


def warp_from_saved(path: Path) -> dict:
    """The old radial example (radii in px at ref_zoom per angle bin, one row per contour) as
    a field: minutes interpolated along each ray between contours, past the last no data."""
    from server.warp import field as F
    old = json.loads(path.read_text())
    o = old["origin"]
    m_per_px = F.metres_per_unit(o["lat"]) / (512 * 2 ** old["ref_zoom"])
    rows = np.array(old["source_radii"], dtype=np.float64) * m_per_px
    mins = np.array(old["contour_minutes"], dtype=np.float64) * MAPBOX_WALK_KMH / PAGE_WALK_KMH
    n = rows.shape[1]
    grid = F.Grid.covering(100.0, -rows.max() - 1000, -rows.max() - 1000, rows.max() + 1000, rows.max() + 1000)
    X, Y = grid.mesh()
    r, th = np.hypot(X, Y), np.arctan2(Y, X)
    f = (th + np.pi) / (2 * np.pi) * n - 0.5
    i0 = np.floor(f).astype(int) % n
    a = f - np.floor(f)
    radius = rows[:, i0] * (1 - a) + rows[:, (i0 + 1) % n] * a          # (contours, ny, nx)
    t = np.full(X.shape, np.nan)
    r0, m0 = np.zeros(X.shape), np.zeros(X.shape)
    for k in range(len(mins)):
        inside = np.isnan(t) & (r <= radius[k])
        t[inside] = (m0 + (mins[k] - m0) * (r - r0) / np.maximum(radius[k] - r0, 1e-6))[inside]
        r0, m0 = radius[k], np.full(X.shape, mins[k])
    minutes, filled, grid = F.crop(t, np.zeros(t.shape, bool), grid, 1000)
    oj, oi = grid.origin_index()
    minutes[oj, oi] = 0.0
    ex = {"origin": {"lon": o["lon"], "lat": o["lat"], "label": o["label"]}, "mode": "walking",
          "window": None, "window_label": None, **grid.header(), "minutes_b64": F.encode(minutes, filled),
          "sources": "mapbox", "limit_minutes": 60, "approximate": True, "valid_until": None,
          "notes": ["saved_example"], "credits": [], "development_only": False}
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
    parser.add_argument("base_url", nargs="?")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "static")
    parser.add_argument("--only", choices=["warp", "meetup"], help="make just one of the two")
    parser.add_argument("--from-saved", type=Path, help="write the warp example from the old radial example, offline")
    args = parser.parse_args()

    made = {}
    if args.from_saved:
        made["warp"] = warp_from_saved(args.from_saved)
    elif not args.base_url:
        parser.error("give a server URL, or --from-saved")
    else:
        with httpx.Client(base_url=args.base_url, timeout=120) as client:
            if args.only in (None, "warp"):
                made["warp"] = make_warp(client)
            if args.only in (None, "meetup"):
                made["meetup"] = make_meetup(client)

    for sub, data in made.items():
        path = args.out_dir / sub / ("example-field.json" if sub == "warp" else "example.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, separators=(",", ":")) + "\n")
        print(f"wrote {path} ({path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
