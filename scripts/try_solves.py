"""Run fixed meetup groups against a server and print what came back.

For reproducing problems and for before/after numbers. Start a server first, e.g.

    ROUTER_PROVIDER=euclidean MAPBOX_TILES_TOKEN=x MAPBOX_ROUTING_TOKEN=x \
        uvicorn server.app:app --port 8000
    python scripts/try_solves.py http://localhost:8000

Against live Mapbox, cap the spend on the server you run it against
(MATRIX_DAILY_BUDGET=8000, CLIENT_DAILY_SHARE=1 so one client may use all of it, and a scratch
USAGE_DB_PATH): every solve spends Matrix elements. --repeat N runs each group N times and
prints the median and p95 wall time of the solves that answered 200, with the count of each
status; --pause S waits between solves so the Matrix rate window (60 requests a minute) is
not what is measured.

The groups are public landmarks, never anybody's home.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter

import httpx

GROUPS = {
    # The issue's example: two walkers far apart.
    "G1": [
        {"id": "wembley", "lon": -0.2795, "lat": 51.5635, "mode": "walking"},
        {"id": "bank", "lon": -0.0886, "lat": 51.5133, "mode": "walking"},
    ],
    # The saved example (scripts/make_examples.py).
    "G2": [
        {"id": "angel", "lon": -0.1058, "lat": 51.5322, "mode": "cycling"},
        {"id": "vauxhall", "lon": -0.1253, "lat": 51.4861, "mode": "cycling"},
        {"id": "marble-arch", "lon": -0.1589, "lat": 51.5131, "mode": "cycling"},
    ],
    # Three modes by the Thames: many grid centres fall in the river.
    "G3": [
        {"id": "rotherhithe", "lon": -0.0525, "lat": 51.5009, "mode": "walking"},
        {"id": "canary-wharf", "lon": -0.0235, "lat": 51.5054, "mode": "cycling"},
        {"id": "pimlico", "lon": -0.1334, "lat": 51.4893, "mode": "driving"},
    ],
}

DIAG_KEYS = ("stage", "coarse_resolution", "fine_resolution", "answer_resolution", "coarse_candidates",
             "fine_candidates", "feasible_count", "dropped_no_route", "unchecked_for_budget",
             "dropped_off_street", "retries", "bad_replies", "matrix_requests", "matrix_elements",
             "estimated_matrix_elements", "elapsed_ms")


def solve_once(client: httpx.Client, name: str, objective: str) -> dict:
    body = {"participants": GROUPS[name], "objective": objective, "search": {"top_k": 4}}
    started = time.perf_counter()
    resp = client.post("/api/meetup/solve", json=body)
    wall = time.perf_counter() - started
    row = {"group": name, "status": resp.status_code, "wall_s": round(wall, 2)}
    try:
        data = resp.json()
    except ValueError:
        row["error"] = "not JSON"
        return row
    if resp.is_success:
        best = data["best"]
        row["best"] = {k: best.get(k) for k in ("lon", "lat", "name", "snap_m", "score",
                                                "max_time_min", "mean_time_min")}
        row["diagnostics"] = {k: data["diagnostics"][k] for k in DIAG_KEYS if k in data["diagnostics"]}
        row["notes"] = data.get("notes", [])
    else:
        row["error"] = {k: data.get(k) for k in ("code", "detail", "reason", "who") if k in data}
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base_url")
    parser.add_argument("--group", choices=sorted(GROUPS), action="append",
                        help="run only this group (repeatable); default: all")
    parser.add_argument("--objective", choices=["minimax", "sum"], default="minimax")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--pause", type=float, default=0.0, help="seconds to wait between solves")
    args = parser.parse_args()

    names = args.group or sorted(GROUPS)
    walls: dict[str, list[float]] = {n: [] for n in names}   # solves that answered 200
    statuses: Counter[int] = Counter()
    first = True
    with httpx.Client(base_url=args.base_url, timeout=120) as client:
        for _ in range(args.repeat):
            for name in names:
                if not first and args.pause:
                    time.sleep(args.pause)
                first = False
                row = solve_once(client, name, args.objective)
                statuses[row["status"]] += 1
                if row["status"] == 200:
                    walls[name].append(row["wall_s"])
                print(json.dumps(row))

    if args.repeat > 1:
        print("statuses: " + ", ".join(f"{code}: {n}" for code, n in sorted(statuses.items())))
        for name, ws in walls.items():
            if ws:
                print(f"{name}: median {statistics.median(ws):.2f} s, slowest {max(ws):.2f} s, n={len(ws)}")
        everything = sorted(w for ws in walls.values() for w in ws)
        total = sum(statuses.values())
        if len(everything) < total:
            print(f"only {len(everything)} of {total} solves answered 200: the timings below are "
                  "not a p95 of this run")
        if everything:
            # Nearest-rank p95: with fewer than 20 samples this is the slowest one.
            p95 = everything[max(0, -(-95 * len(everything) // 100) - 1)]
            print(f"answered: median {statistics.median(everything):.2f} s, p95 {p95:.2f} s, "
                  f"n={len(everything)}")


if __name__ == "__main__":
    main()
