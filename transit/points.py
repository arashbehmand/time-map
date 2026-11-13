"""
Table points: every rail, tube, DLR and tram station at its real position, then the H3
res-8 cells whose centres lie in Greater London + 2 km. Stations first (sorted by id), then
cells (sorted), so the order is stable from build to build.
"""
from __future__ import annotations

import json
from pathlib import Path

import h3
import numpy as np
import pandas as pd

from transit import settings
from transit.feeds import active_services, agency_of_trips


def load_region(path: Path = settings.REGION_GEOJSON):
    from shapely.geometry import shape
    from shapely.ops import unary_union

    with open(path) as f:
        gj = json.load(f)
    return unary_union([shape(feat["geometry"]) for feat in gj["features"]])


def buffered(region, metres: float):
    """region (lon/lat) buffered by `metres` in British National Grid, back to lon/lat."""
    from pyproj import Transformer
    from shapely.ops import transform

    to_bng = Transformer.from_crs(4326, settings.BNG_EPSG, always_xy=True).transform
    to_wgs = Transformer.from_crs(settings.BNG_EPSG, 4326, always_xy=True).transform
    return transform(to_wgs, transform(to_bng, region).buffer(metres))


def is_station_type(route_type: int) -> bool:
    return route_type in settings.STATION_ROUTE_TYPES or any(
        lo <= route_type <= hi for lo, hi in settings.STATION_ROUTE_TYPE_RANGES)


def station_points(feeds: dict[str, dict], dates, polygon) -> pd.DataFrame:
    """One point per parent station (or parentless stop) served by rail/subway/tram trips."""
    import shapely

    rows = []
    for fid, feed in feeds.items():
        t = agency_of_trips(feed)
        t = t[t["route_type"].map(is_station_type)]
        act = active_services(feed, dates)
        running = set().union(*act.values())
        t = t[t["service_id"].isin(running)]
        st = feed["stop_times"]
        served = set(st.loc[st["trip_id"].isin(set(t["trip_id"])), "stop_id"])
        stops = feed["stops"].set_index("stop_id")
        parent = stops["parent_station"] if "parent_station" in stops.columns else pd.Series("", stops.index)
        stations = sorted({(parent.get(s) or s) for s in served})
        for sid in stations:
            if sid not in stops.index:
                continue
            rec = stops.loc[sid]
            rows.append({"kind": "station", "id": f"{fid}:{sid}", "name": rec["stop_name"],
                         "lon": float(rec["stop_lon"]), "lat": float(rec["stop_lat"])})
    df = pd.DataFrame(rows, columns=["kind", "id", "name", "lon", "lat"])
    if len(df):
        df = df[shapely.contains_xy(polygon, df["lon"].to_numpy(), df["lat"].to_numpy())]
    return df.sort_values("id").reset_index(drop=True)


def cell_points(polygon, res: int = settings.H3_RES_POINTS) -> pd.DataFrame:
    """H3 cells whose centres lie inside polygon (lon/lat)."""
    cells = sorted(h3.geo_to_cells(polygon, res))
    latlng = np.array([h3.cell_to_latlng(c) for c in cells]) if cells else np.zeros((0, 2))
    return pd.DataFrame({"kind": "cell", "id": cells, "name": "",
                         "lon": latlng[:, 1], "lat": latlng[:, 0]})


def build_points(feeds: dict[str, dict], dates, region) -> tuple[pd.DataFrame, dict]:
    import shapely

    area = buffered(region, settings.BUFFER_POINTS_M)
    stations = station_points(feeds, dates, area)
    cells = cell_points(area)
    lo, hi = settings.STATION_COUNT_RANGE
    if not lo <= len(stations) <= hi:
        raise SystemExit(f"{len(stations)} stations, outside the expected {lo}-{hi}: check the feeds")
    lo, hi = settings.CELL_COUNT_RANGE
    if not lo <= len(cells) <= hi:
        raise SystemExit(f"{len(cells)} H3 cells, outside the expected {lo}-{hi}: check the boundary")
    pts = pd.concat([stations, cells], ignore_index=True)
    pts.insert(0, "idx", np.arange(len(pts)))
    pts["inside_gl"] = shapely.contains_xy(region, pts["lon"].to_numpy(), pts["lat"].to_numpy())
    pts["snap_m"] = np.nan
    pts["ok"] = True
    return pts, {"stations": int(len(stations)), "cells": int(len(cells)), "total": int(len(pts))}


def buffer_gaps(points: pd.DataFrame, feeds: dict[str, dict], radius_m: float = 800) -> dict:
    """Share of points outside Greater London with no stop within radius_m."""
    from transit.tables import haversine_m

    lons, lats = [], []
    for feed in feeds.values():
        s = feed["stops"]
        lons.append(pd.to_numeric(s["stop_lon"], errors="coerce").to_numpy())
        lats.append(pd.to_numeric(s["stop_lat"], errors="coerce").to_numpy())
    slon, slat = np.concatenate(lons), np.concatenate(lats)
    ring = points[~points["inside_gl"]]
    gaps = 0
    for lon, lat in zip(ring["lon"], ring["lat"]):
        near = (np.abs(slat - lat) < 0.01) & (np.abs(slon - lon) < 0.016)
        if not near.any() or haversine_m(lon, lat, slon[near], slat[near]).min() > radius_m:
            gaps += 1
    return {"buffer_points": int(len(ring)), "no_stop_within_m": radius_m, "gaps": gaps,
            "share": (gaps / len(ring)) if len(ring) else 0.0}
