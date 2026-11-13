"""No timetable feed, map extract or travel-time table is ever tracked by git: only code and
docs go to GitHub. The source timetables must never be published (docs/transit-sources.md)."""
from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ("*.zip", "*.npy", "*.npz", "*.pbf", "*.osm", "*.parquet", "*.sqlite3", "*.mca", "*.cif",
        "rjtt*", "tfl_*.xml", "agency.txt", "calendar*.txt", "feed_info.txt", "frequencies.txt", "routes.txt",
        "shapes.txt", "stop_times.txt", "stops.txt", "transfers.txt", "trips.txt")
MAX_BYTES = 1_000_000
LARGE_OK = {"transit/london_region.geojson"}


def _git(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    except FileNotFoundError:
        pytest.skip("git is not installed")


def _tracked() -> list[str]:
    out = _git("ls-files", "-z")
    if out.returncode:
        pytest.skip("not a git checkout")
    return [p for p in out.stdout.split("\0") if p]


def test_no_data_or_large_file_is_tracked():
    files = _tracked()
    data = [p for p in files if any(fnmatch.fnmatch(Path(p).name.lower(), d) for d in DATA)
            or (p.startswith("transit/") and p.lower().endswith(".xml"))]   # TransXChange
    assert not data, f"timetable or table files tracked by git: {data}"
    big = [p for p in files if p not in LARGE_OK and (ROOT / p).is_file()
           and (ROOT / p).stat().st_size > MAX_BYTES]
    assert not big, f"files over {MAX_BYTES} bytes tracked by git: {big}"


@pytest.mark.parametrize("path", ["some/dir/feed.zip", "x/RJTTF999.MCA", "x/tt_wd08.npy",
                                  "x/walksheds.npz", "x/london.osm.pbf", "gtfs/stop_times.txt",
                                  "gtfs/calendar_dates.txt", "x/tfl.sqlite3",
                                  "x/tfl_1-BAK-_-y05-2116201.xml", "transit/scratch/route.xml"])
def test_data_files_are_ignored_anywhere(path):
    if _git("rev-parse", "--git-dir").returncode:
        pytest.skip("not a git checkout")
    assert _git("check-ignore", "-q", "--no-index", path).returncode == 0, f"{path} is not ignored"
