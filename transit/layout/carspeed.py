"""
Typical weekday car speeds for London at 13:00-14:00 (#23, L-03), from Traffic4cast 2022.

LONDON_2022.zip (HERE's sample data, Traffic4cast 2022) holds, for 110 days from 2019-07-01 to
2020-01-31, one `speed_classes/london/speed_classes_<date>.parquet` per day: the MeTS-10
pipeline's speeds on OpenStreetMap edges, one row per directed edge (u, v: OSM node ids) and
15-minute bin (t, 0-95) that had enough probe data. Columns: u, v, day, t, volume_class,
median_speed_kph, free_flow_kph. The zip has no road geometry; transit/layout/car.py puts the
edges on our own roads by their node ids.

What is kept, per directed edge: the median, over every kept (day, bin) row, of
median_speed_kph, where
- the day is a weekday, not one of England's bank holidays, not 23 December to 3 January, and
  before March 2020 (a later day is refused: the covid period);
- the bin is 13:00-14:00 (t = 52-55, the transit tables' wd13 window; the bins follow London's
  clock, see CLOCK);
- the bin's median is at least 1 km/h.

Read member by member from inside the zip (the zip is never unpacked or moved). Writes, into
the zip's own data root (`<data>/work/t4c22/`, git-ignored): london_wd13_speeds.parquet (u, v,
median_kph, rows) and london_wd13_speeds.json (what was read and kept, the zip's sha256 and the
licence). Rerun only when the zip or these rules change.

Licence (recorded in the report, LICENCE): HERE gives the sample data for academic and
non-commercial use; the owner uses it non-commercially, with the permissions collected.

    python -m transit.layout.carspeed [--t4c-zip PATH] [--force]

pyarrow (transit/requirements.txt); not the app's environment.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from datetime import date
from pathlib import Path

import numpy as np

from transit import settings
from transit.layout.prep import RefusedInput, sha256

SPEEDS = "london_wd13_speeds.parquet"
META = "london_wd13_speeds.json"
LICENCE = ("HERE's sample data (Traffic4cast 2022), for academic and non-commercial use only: requesting it "
           "means agreeing to that, and HERE's standard terms cover any other use "
           "(https://developer.here.com/sample-data). The speeds on OpenStreetMap edges are from the MeTS-10 "
           "pipeline (https://github.com/iarai/MeTS-10, Apache-2.0 code; arXiv:2302.08761). Map data © "
           "OpenStreetMap contributors (ODbL).")
CLOCK = ("The bins follow London's clock: 13:00-14:00 is t = 52-55. Traffic4cast's documentation doesn't say which "
         "clock they use, but the morning rush (the slowest bin, edge by edge against its own median) is at "
         "t = 35, 08:45, both on 2019-07-02 (summer time) and on 2019-12-17 (GMT); UTC bins would put the summer "
         "one an hour earlier.")


def cache_dir(zip_path: Path) -> Path:
    """`<data>/work/t4c22/` for a zip at `<data>/raw/t4c22/LONDON_2022.zip`; beside the zip otherwise."""
    zip_path = Path(zip_path).resolve()
    parts = zip_path.parts
    if len(parts) >= 4 and parts[-3] == "raw":
        return zip_path.parents[2] / "work" / "t4c22"
    return zip_path.parent / "work"


def day_rule(d: date) -> str | None:
    """Why day d is left out, or None when it is used. A day after T4C_LAST_DAY is refused, not
    left out (see aggregate)."""
    if d.weekday() >= 5:
        return "weekend"
    if d in settings.T4C_HOLIDAYS:
        return "bank holiday"
    (m0, d0), (m1, d1) = settings.T4C_CHRISTMAS
    if (d.month, d.day) >= (m0, d0) or (d.month, d.day) <= (m1, d1):
        return "Christmas weeks"
    return None


def _member_day(name: str) -> date | None:
    stem = Path(name).name
    if not (stem.startswith("speed_classes_") and stem.endswith(".parquet")):
        return None
    try:
        return date.fromisoformat(stem[len("speed_classes_"):-len(".parquet")])
    except ValueError:
        return None


def group_median(u: np.ndarray, v: np.ndarray, s: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """The median of s per (u, v), exactly: (u, v, median, rows), sorted by (u, v)."""
    if not len(s):
        z = np.zeros(0, dtype=np.int64)
        return z, z, np.zeros(0), z
    order = np.lexsort((s, v, u))
    u, v, s = u[order], v[order], s[order]
    new = np.ones(len(s), dtype=bool)
    new[1:] = (u[1:] != u[:-1]) | (v[1:] != v[:-1])
    start = np.where(new)[0]
    n = np.diff(np.append(start, len(s)))
    lo = start + (n - 1) // 2
    hi = start + n // 2
    return u[start], v[start], (s[lo] + s[hi]) / 2, n


def aggregate(zip_path: Path, log=print) -> tuple[dict, dict]:
    """Read the zip; returns (columns {u, v, median_kph, rows}, meta)."""
    import pyarrow.parquet as pq

    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise RefusedInput(f"{zip_path} is missing: download LONDON_2022.zip from HERE's sample data "
                           "(https://developer.here.com/sample-data) and put it there, or pass --t4c-zip "
                           "(or set T4C_ZIP)")
    want = set(settings.T4C_COLUMNS)
    us, vs, ss = [], [], []
    used, skipped, vol = [], {}, {}
    rows_read = rows_window = rows_slow = 0
    with zipfile.ZipFile(zip_path) as z:
        members = sorted(n for n in z.namelist() if n.startswith(settings.T4C_MEMBER_DIR) and _member_day(n))
        if not members:
            raise RefusedInput(f"{zip_path.name} has no {settings.T4C_MEMBER_DIR}speed_classes_<date>.parquet")
        late = [n for n in members if _member_day(n) > settings.T4C_LAST_DAY]
        if late:
            raise RefusedInput(f"{zip_path.name} has days after {settings.T4C_LAST_DAY} (the covid period): "
                               f"{', '.join(Path(n).name for n in late[:3])}")
        for name in members:
            d = _member_day(name)
            why = day_rule(d)
            if why:
                skipped[d.isoformat()] = why
                continue
            with z.open(name) as f:
                table = pq.read_table(io.BytesIO(f.read()))
            cols = set(table.column_names)
            if cols != want:
                raise RefusedInput(f"{Path(name).name} has the columns {sorted(cols)}, not {sorted(want)}: the "
                                   "speed files are not the ones transit/layout/carspeed.py reads")
            t = table.column("t").to_numpy()
            rows_read += len(t)
            keep = np.isin(t, settings.T4C_BINS)
            days = set(table.column("day").to_numpy()[keep].tolist()) if keep.any() else set()
            if days - {d.isoformat()}:
                raise RefusedInput(f"{Path(name).name} holds rows for {sorted(days)}, not only {d}")
            s = table.column("median_speed_kph").to_numpy()[keep]
            rows_window += len(s)
            ok = np.isfinite(s) & (s >= settings.CAR_MIN_KPH)
            rows_slow += int((~ok).sum())
            vc = table.column("volume_class").to_numpy()[keep][ok]
            for c, n in zip(*np.unique(vc, return_counts=True)):
                vol[str(int(c))] = vol.get(str(int(c)), 0) + int(n)
            us.append(table.column("u").to_numpy()[keep][ok].astype(np.int64))
            vs.append(table.column("v").to_numpy()[keep][ok].astype(np.int64))
            ss.append(s[ok].astype(np.float64))
            used.append(d.isoformat())
            log(f"  {d}: {int(ok.sum())} rows at 13:00-14:00")
    if not used:
        raise RefusedInput(f"{zip_path.name}: no weekday is left to use")
    u, v, med, n = group_median(np.concatenate(us), np.concatenate(vs), np.concatenate(ss))
    kept = int(n.sum())
    synthetic = int(((u >= settings.T4C_SYNTHETIC_ID) | (v >= settings.T4C_SYNTHETIC_ID)).sum())
    meta = {
        "zip": str(zip_path), "zip_bytes": zip_path.stat().st_size, "zip_mtime": zip_path.stat().st_mtime,
        "zip_sha256": sha256(zip_path), "licence": LICENCE, "clock": CLOCK,
        "bins": list(settings.T4C_BINS), "window": "13:00-14:00",
        "days_in_zip": len(members), "days_used": used, "days_skipped": skipped,
        "rules": {"weekdays": True, "holidays": [h.isoformat() for h in settings.T4C_HOLIDAYS],
                  "christmas": [list(x) for x in settings.T4C_CHRISTMAS],
                  "last_day": settings.T4C_LAST_DAY.isoformat(), "min_kph": settings.CAR_MIN_KPH},
        "rows_read": rows_read, "rows_in_window": rows_window, "rows_under_min_kph": rows_slow, "rows_kept": kept,
        "volume_class_rows": dict(sorted(vol.items())), "edges": int(len(u)), "edges_with_synthetic_id": synthetic,
        "median_kph_quartiles": [float(x) for x in np.percentile(med, [25, 50, 75])] if len(med) else [],
    }
    return {"u": u, "v": v, "median_kph": med, "rows": n}, meta


def write(cols: dict, meta: dict, out: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({k: cols[k] for k in ("u", "v", "median_kph", "rows")}), out / SPEEDS)
    (out / META).write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")


def load(zip_path: Path | None = None, out: Path | None = None) -> tuple[dict, dict]:
    """The cached speeds and their meta, refused when missing or made from another zip (its size
    and modification time differ from the meta's)."""
    import pyarrow.parquet as pq

    zip_path = Path(zip_path or settings.T4C_ZIP)
    out = Path(out or cache_dir(zip_path))
    if not (out / SPEEDS).is_file() or not (out / META).is_file():
        raise RefusedInput(f"no car speeds in {out}: run python -m transit.layout.carspeed --t4c-zip {zip_path}")
    meta = json.loads((out / META).read_text(encoding="utf-8"))
    if zip_path.is_file() and (meta.get("zip_bytes") != zip_path.stat().st_size
                               or meta.get("zip_mtime") != zip_path.stat().st_mtime):
        raise RefusedInput(f"the car speeds in {out} were made from another {zip_path.name}: run "
                           "python -m transit.layout.carspeed --force")
    t = pq.read_table(out / SPEEDS)
    cols = {k: t.column(k).to_numpy() for k in ("u", "v", "median_kph", "rows")}
    return cols, meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m transit.layout.carspeed",
                                 description="Weekday 13:00-14:00 car speeds from LONDON_2022.zip (#23, L-03).")
    ap.add_argument("--t4c-zip", type=Path, default=settings.T4C_ZIP, help="default: $T4C_ZIP or "
                    "$TRANSIT_DATA/raw/t4c22/LONDON_2022.zip")
    ap.add_argument("--out", type=Path, help="default: <data>/work/t4c22/ beside the zip's raw/ folder")
    ap.add_argument("--force", action="store_true", help="rebuild even if the cache matches the zip")
    args = ap.parse_args(argv)
    out = args.out or cache_dir(args.t4c_zip)
    try:
        if not args.force:
            try:
                _, meta = load(args.t4c_zip, out)
                print(f"car speeds already in {out} ({meta['edges']} edges, {len(meta['days_used'])} days)")
                return 0
            except RefusedInput:
                pass
        cols, meta = aggregate(args.t4c_zip)
        write(cols, meta, out)
    except RefusedInput as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(f"{meta['edges']} directed edges with a 13:00-14:00 speed from {len(meta['days_used'])} weekdays "
          f"({meta['days_used'][0]} to {meta['days_used'][-1]}); median {meta['median_kph_quartiles'][1]:.1f} km/h; "
          f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
