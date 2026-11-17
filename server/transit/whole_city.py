"""
Whole city (#28, U-03): the full London time map of #6 (L-02), served by the app.

`python -m transit.layout.timemap --build <id>` writes one folder per timetable window,
transit/out/timemap/<build>/<window>/, holding what the page draws (timemap.json, land.bin) and
the build's report (report.md, report.json, shepard.svg, panels.svg). All of it is derived from
the transit tables, so it stays out of git and out of static/ (tests/test_no_data_in_git.py),
and TIMEMAP_DIR points the app at one build's folder.

It is published only under the tables' own gate, not a copy of it: a window is served only
while the loaded tables (server/transit/provider.py) may be served to the same request
(unexpired; licences cleared, or TRANSIT_ALLOW_UNCLEARED for this machine only), and only if
it was made from exactly those tables: the same build, and the same table file (its sha256).
So withdrawing or expiring the tables withdraws the map with them.

Nothing here imports transit.layout: the image ships only transit/__init__.py and tables.py.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from server.transit.provider import WINDOWS, TransitHolder, london_today, window_label

log = logging.getLogger("time_map.whole_city")

REPO = Path(__file__).resolve().parents[2]
SHIPPED = ("static", "server")      # what the app serves or ships whole: never data
SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'"
# Only what the page fetches or links: the map, its land, the report and the Shepard plot (#6).
FILES = {
    "timemap.json": "application/json",
    "land.bin": "application/octet-stream",
    "report.md": "text/plain; charset=utf-8",
    "shepard.svg": "image/svg+xml",
}
REPORT = ("report.md", "shepard.svg")     # both present: the page links "How this map was checked"
# The travel modes (#23, L-03), in the window's modes/ folder: car alone and car with train and tube,
# served like the map itself, under the same gate.
MODES_DIR = "modes"
MODE_FILES = {
    "modes.json": "application/json",
    "combo-car.json": "application/json",
    "combo-car-train.json": "application/json",
    "roads.bin": "application/octet-stream",
    "report.md": "text/plain; charset=utf-8",
}
DTYPE_BYTES = {"uint8": 1, "uint16": 2, "uint32": 4, "int32": 4, "float32": 4}
GZIP_MIN = 500                            # as the app's GZipMiddleware


class WholeCityUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Blob:
    """One file, and its gzip copy made once at load: the files are megabytes, and compressing
    them on every view would cost the one process that serves everything else."""
    body: bytes
    etag: str
    media_type: str
    gz: bytes | None = None
    gz_etag: str = ""


def _blob(body: bytes, name: str, media_type: str | None = None) -> Blob:
    tag = hashlib.sha256(body).hexdigest()[:32]
    gz = gzip.compress(body, compresslevel=6, mtime=0) if len(body) >= GZIP_MIN else None
    if gz is not None and len(gz) >= len(body):
        gz = None
    return Blob(body, f'"{tag}"', media_type or FILES[name], gz, f'"{tag}-gz"' if gz is not None else "")


@dataclass(frozen=True)
class CityWindow:
    window: str
    build: str
    valid_until: date
    table_sha256: str
    files: dict[str, Blob]
    modes: dict[str, Blob] = field(default_factory=dict)

    @property
    def report(self) -> bool:
        return all(n in self.files for n in REPORT)


def _land_bytes(land: dict) -> int:
    """The smallest land.bin its header can describe: the end of its last array."""
    end = 0
    for a in land.get("arrays", []):
        end = max(end, int(a["offset"]) + int(a["length"]) * DTYPE_BYTES[a["dtype"]])
    return end


def load_window(d: Path) -> CityWindow:
    """One window's folder, checked and read into memory."""
    try:
        raw = (d / "timemap.json").read_bytes()
        doc = json.loads(raw)
    except (OSError, ValueError):
        raise WholeCityUnavailable("missing") from None
    if not isinstance(doc, dict):
        raise WholeCityUnavailable("bad_doc")
    try:
        ok = (doc.get("schema") == 2 and doc.get("kind") == "london-time-map"
              and doc.get("window") == d.name and isinstance(doc.get("build"), str) and doc["build"]
              and isinstance(doc.get("table_sha256"), str) and len(doc["table_sha256"]) == 64)
        valid_until = date.fromisoformat(doc["valid_until"]) if ok else None
    except (KeyError, TypeError, ValueError):
        ok = False
    if not ok:
        raise WholeCityUnavailable("bad_doc")
    files = {"timemap.json": _blob(raw, "timemap.json")}
    land = doc.get("land")
    try:
        body = (d / "land.bin").read_bytes()
    except OSError:
        body = None
    if land:
        try:
            need = _land_bytes(land)
        except (KeyError, TypeError, ValueError):
            raise WholeCityUnavailable("bad_doc") from None
        if body is None or len(body) < need:
            raise WholeCityUnavailable("missing")
    if body is not None:
        files["land.bin"] = _blob(body, "land.bin")
    for name in REPORT:
        try:
            files[name] = _blob((d / name).read_bytes(), name)
        except OSError:
            pass
    return CityWindow(d.name, doc["build"], valid_until, doc["table_sha256"], files, _load_modes(d, doc))


def _load_modes(d: Path, doc: dict) -> dict[str, Blob]:
    """The window's travel modes, every file present and made with this map (the same build, window,
    table and node count), or none."""
    md = d / MODES_DIR
    if not (md / "modes.json").is_file():
        return {}
    nodes = len((doc.get("nodes") or {}).get("kind") or [])
    out = {}
    try:
        for name, media in MODE_FILES.items():
            body = (md / name).read_bytes()
            if name.endswith(".json"):
                m = json.loads(body)
                same = (isinstance(m, dict) and m.get("build") == doc["build"] and m.get("window") == d.name
                        and m.get("table_sha256") == doc["table_sha256"] and m.get("nodes") == nodes)
                if not same:
                    log.warning("whole city: window %s's travel modes left out (%s was made with another map)",
                                d.name, name)
                    return {}
            out[name] = _blob(body, name, media)
    except (OSError, ValueError) as exc:
        log.warning("whole city: window %s's travel modes left out (%s)", d.name, type(exc).__name__)
        return {}
    return out


def load_dir(directory: str | Path, repo: Path = REPO) -> dict[str, CityWindow]:
    """Every window folder under one build's time-map folder that loads; refused inside the
    folders the app serves or ships."""
    d = Path(directory)
    real = d.resolve()
    try:
        rel = real.relative_to(repo)
        if rel.parts and rel.parts[0] in SHIPPED:
            raise WholeCityUnavailable("refused_here")
    except ValueError:
        pass
    if not real.is_dir():
        raise WholeCityUnavailable("missing")
    out = {}
    for w in WINDOWS:
        if not (real / w / "timemap.json").is_file():
            continue
        try:
            out[w] = load_window(real / w)
        except WholeCityUnavailable as exc:
            log.warning("whole city: window %s left out (%s)", w, exc.reason)
        except Exception as exc:  # noqa: BLE001 - one broken window must not cost the others
            log.warning("whole city: window %s left out (bad_doc: %s)", w, type(exc).__name__)
    return out


class WholeCityHolder:
    """What the app keeps: the loaded windows, or why there are none."""

    def __init__(self, configured: bool):
        self.configured = configured
        self.loading = configured
        self.windows: dict[str, CityWindow] = {}
        self.reason = "loading" if configured else "not_configured"
        self._told: set[tuple] = set()     # windows left out against the tables, logged once each

    def load(self, directory: str, repo: Path = REPO) -> None:
        try:
            self.windows = load_dir(directory, repo)
            self.reason = "" if self.windows else "missing"
            if self.windows:
                size = sum(len(b.body) for w in self.windows.values() for b in w.files.values())
                log.info("whole city: %s loaded (%s), %.1f MB",
                         ", ".join(sorted(self.windows)), next(iter(self.windows.values())).build, size / 1e6)
            else:
                log.warning("whole city: no window loaded from TIMEMAP_DIR")
        except WholeCityUnavailable as exc:
            self.reason = exc.reason
            log.warning("whole city: not available (%s)", exc.reason)
        except Exception as exc:  # noqa: BLE001 - a bad folder must never stop the app
            self.reason = "load_failed"
            log.warning("whole city: load failed (%s)", type(exc).__name__)
        finally:
            self.loading = False

    def usable(self, transit: TransitHolder, *, local: bool = False,
               today: date | None = None) -> dict[str, CityWindow]:
        """The windows that may be served to this request: made from the tables that may be."""
        if not self.windows:
            return {}
        data = transit.usable(today, local=local)
        if data is None:
            return {}
        outputs = data.manifest.get("outputs", {})
        today = today or london_today()
        out = {}
        for w, cw in self.windows.items():
            why = ("made from build " + cw.build if cw.build != data.build
                   else "made from another table" if cw.table_sha256 != (outputs.get(f"tt_{w}") or {}).get("sha256")
                   else f"expired on {cw.valid_until}" if cw.valid_until < today else "")
            if not why:
                out[w] = cw
            elif (w, data.build, why) not in self._told:
                # Once per window and reason: the operator learns a rebuild of the tables left the
                # time map behind (#30), without a line on every request.
                self._told.add((w, data.build, why))
                log.warning("whole city: window %s not served with tables %s: %s", w, data.build, why)
        return out

    def reason_for(self, transit: TransitHolder, *, local: bool = False, today: date | None = None) -> str:
        if not self.configured:
            return "not_configured"
        if self.loading:
            return "loading"
        if not self.windows:
            return self.reason or "missing"
        data = transit.usable(today, local=local)
        if data is None:
            return "loading" if transit.loading else transit.reason_for(local)
        today = today or london_today()
        same = [cw for cw in self.windows.values() if cw.build == data.build]
        if same and all(cw.valid_until < today for cw in same):
            return "expired"
        return "build_mismatch"

    def status(self, transit: TransitHolder, *, local: bool = False, today: date | None = None) -> dict:
        windows = self.usable(transit, local=local, today=today)
        if not windows:
            return {"available": False, "reason": self.reason_for(transit, local=local, today=today)}
        data = transit.usable(today, local=local)
        first = next(iter(windows.values()))
        return {
            "available": True, "reason": "", "build": first.build,
            "valid_until": min(w.valid_until for w in windows.values()).isoformat(),
            "windows": [{"id": w, "label": window_label(data.windows[w])} for w in WINDOWS if w in windows],
            "report": {w: cw.report for w, cw in windows.items()},
            "development_only": data.development_only,
            # The travel modes (#23): the windows built with them.
            **({"modes": {w: True for w, cw in windows.items() if cw.modes}}
               if any(cw.modes for cw in windows.values()) else {}),
        }

    def file(self, transit: TransitHolder, window: str, name: str, *, local: bool = False) -> Blob | None:
        cw = self.usable(transit, local=local).get(window)
        return cw.files.get(name) if cw else None

    def mode_file(self, transit: TransitHolder, window: str, name: str, *, local: bool = False) -> Blob | None:
        """A travel-mode file, under the same gate as the map."""
        cw = self.usable(transit, local=local).get(window)
        return cw.modes.get(name) if cw else None
