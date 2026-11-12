import logging
import os

from dotenv import load_dotenv

load_dotenv()

_log = logging.getLogger("time_map.config")

_LEGACY_TOKEN = os.environ.get("MAPBOX_ACCESS_TOKEN", "")

# Tiles and glyphs (browser-facing map data, proxied by us).
MAPBOX_TILES_TOKEN: str = os.environ.get("MAPBOX_TILES_TOKEN", "") or _LEGACY_TOKEN
# Matrix and Isochrone (server-side routing).
MAPBOX_ROUTING_TOKEN: str = os.environ.get("MAPBOX_ROUTING_TOKEN", "") or _LEGACY_TOKEN

if not (MAPBOX_TILES_TOKEN and MAPBOX_ROUTING_TOKEN):
    raise RuntimeError(
        "Set MAPBOX_TILES_TOKEN and MAPBOX_ROUTING_TOKEN (or MAPBOX_ACCESS_TOKEN for both)."
    )
if MAPBOX_TILES_TOKEN == MAPBOX_ROUTING_TOKEN:
    _log.warning("one Mapbox token serves both tiles and routing; set separate tokens")

# Every secret the log redactor must remove.
SECRETS: tuple[str, ...] = tuple(
    t for t in {_LEGACY_TOKEN, MAPBOX_TILES_TOKEN, MAPBOX_ROUTING_TOKEN} if t
)

# Warp algorithm constants
NUM_ANGULAR_SAMPLES: int = 2048
DENSIFY_PX: float = 4.0
SMOOTH_WINDOW: int = 7
