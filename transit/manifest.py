"""
manifest.json: everything needed to trace a table back to its inputs and settings, and to
know when it stops being valid.
"""
from __future__ import annotations

import json
import platform
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from transit import settings
from transit.fetch import sha256_file

REQUIRED_KEYS = ("schema", "build", "built_at", "timezone", "service_dates", "windows", "inputs",
                 "settings", "versions", "points", "outputs", "valid_until", "attribution")


def valid_until(build_day: date, feed_ends: list[date], days: int = settings.VALID_DAYS) -> date:
    """The earlier of the earliest feed calendar end and build day + `days`."""
    cap = build_day + timedelta(days=days)
    return min([cap, *feed_ends]) if feed_ends else cap


def output_entry(path: Path, **extra) -> dict:
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path), **extra}


def versions() -> dict:
    """Package versions, read from metadata (importing r5py would start a JVM)."""
    from importlib.metadata import PackageNotFoundError, version

    out = {"python": sys.version.split()[0], "platform": platform.platform()}
    for pkg in ("r5py", "JPype1", "h3", "numpy", "pandas", "geopandas", "shapely", "pyproj", "httpx"):
        try:
            out[pkg] = version(pkg)
        except PackageNotFoundError:
            pass
    return out


def build_manifest(build: str, dates: dict, inputs: dict, points: dict, outputs: dict,
                   network: dict, stages: dict, feed_ends: list[date], attribution: list[str],
                   table_settings: dict | None = None) -> dict:
    """table_settings: the settings the tables were built with (from their fingerprints)."""
    built_at = datetime.now(timezone.utc)
    vu = valid_until(date.fromisoformat(build), feed_ends)
    v = versions()
    for k in ("r5py_version", "r5_jar", "java_version", "java_vendor", "max_heap_bytes"):
        if k in network:
            v[k] = network[k]
    return {
        "schema": 1,
        "build": build,
        "built_at": built_at.isoformat(timespec="seconds"),
        "timezone": settings.TIMEZONE,
        "service_dates": dates,
        "windows": {k: {"day": kind, "start": t.strftime("%H:%M"), "minutes": settings.WINDOW_MINUTES}
                    for k, (kind, t) in settings.WINDOWS.items()},
        "inputs": inputs,
        "settings": table_settings if table_settings is not None else settings.snapshot(),
        "versions": v,
        "points": points,
        "outputs": outputs,
        "network": network,
        "stages": stages,
        "encoding": {"dtype": "uint8", "order": "C", "units": "whole minutes, rounded half up",
                     "unreachable": settings.UNREACHABLE,
                     "unreachable_means": f"no time within {settings.MAX_MINUTES} min (R5 does not "
                                          "say whether it is over the limit or has no route)",
                     "diagonal": 0, "rows": "origins", "columns": "destinations",
                     "point_ok": "false where the point could not be snapped to a street"},
        "valid_until": vu.isoformat(),
        "attribution": attribution,
    }


def write(path: Path, manifest: dict) -> None:
    missing = [k for k in REQUIRED_KEYS if k not in manifest]
    if missing:
        raise ValueError(f"manifest lacks {missing}")
    path.write_text(json.dumps(manifest, indent=2, default=str))
