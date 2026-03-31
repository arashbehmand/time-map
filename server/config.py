import os
from dotenv import load_dotenv

load_dotenv()

MAPBOX_TOKEN: str = os.environ.get("MAPBOX_ACCESS_TOKEN", "")
if not MAPBOX_TOKEN:
    raise RuntimeError("Set MAPBOX_ACCESS_TOKEN environment variable")

# Warp algorithm constants
NUM_ANGULAR_SAMPLES: int = 2048
DENSIFY_PX: float = 4.0
SMOOTH_WINDOW: int = 7
DEFAULT_LAYERS: list[str] = [
    "road", "building", "water", "landuse",
    "road_label", "place_label", "poi_label",
]
