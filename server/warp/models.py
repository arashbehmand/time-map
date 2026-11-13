from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

MAX_TRAVEL_TIMES = 4
MAX_MINUTES = 60
WEB_MERCATOR_MAX_LAT = 85.0511


def _unique_sorted(values: list[int]) -> list[int]:
    return sorted(set(values))


# Mapbox Isochrone takes at most 4 contours of at most 60 minutes, so one request is enough.
TravelTimes = Annotated[
    list[Annotated[int, Field(ge=1, le=MAX_MINUTES)]],
    Field(min_length=1, max_length=MAX_TRAVEL_TIMES),
    AfterValidator(_unique_sorted),
]

Profile = Literal["walking", "cycling", "driving"]

_STRICT = ConfigDict(extra="forbid", allow_inf_nan=False)


class IsochroneRequest(BaseModel):
    model_config = _STRICT
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    profile: Profile = "walking"
    travel_times: TravelTimes = Field(default_factory=lambda: [5, 10, 15, 20])


TravelMode = Literal["walking", "cycling", "driving", "transit"]
TransitWindow = Literal["wd08", "wd13", "wd19", "sa13"]


class TravelFieldRequest(BaseModel):
    """POST /api/travel-time-field. `window` is for transit only (default: the window nearest
    the current London time)."""
    model_config = _STRICT
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-WEB_MERCATOR_MAX_LAT, le=WEB_MERCATOR_MAX_LAT)
    mode: TravelMode = "walking"
    window: TransitWindow | None = None
    # Transit only: false keeps the tables' whole minutes (Direction 4 is reported both ways).
    smooth: bool = True
