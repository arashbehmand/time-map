"""
Trips per Underground line per service date: UK2GTFS's conversion against gb-transit's TfL
part (the build's source).

    transit/.venv/bin/python -m transit.uk2gtfs.compare --build <id>
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

from transit.build import Build
from transit.feeds import active_services, agency_of_trips, read_gtfs

LINES = ["Bakerloo", "Central", "Circle", "District", "Hammersmith & City", "Jubilee",
         "Metropolitan", "Northern", "Piccadilly", "Victoria", "Waterloo & City"]


def per_line(feed: dict, dates) -> pd.DataFrame:
    t = agency_of_trips(feed)

    act = active_services(feed, dates)
    rows = {}
    for line in LINES:
        m = t[(t["route_short_name"].str.lower() == line.lower()) |
              (t["route_long_name"].str.lower() == line.lower())]
        rows[line] = {d.isoformat(): int(m["service_id"].isin(act[d]).sum()) for d in dates}
    return pd.DataFrame(rows).T


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--build", required=True)
    args = p.parse_args()
    b = Build(args.build)
    dates = list(b.dates())
    ours = per_line(read_gtfs(b.prepped("gbt")), dates)
    theirs = per_line(read_gtfs(b.work / "uk2gtfs" / "uk2gtfs_underground.zip"), dates)
    table = ours.add_prefix("gb-transit ").join(theirs.add_prefix("UK2GTFS "))
    print(table.to_string())
    (b.out / "uk2gtfs-compare.json").write_text(json.dumps(table.to_dict(), indent=2))


if __name__ == "__main__":
    main()
