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


class WarpParamsRequest(BaseModel):
    model_config = _STRICT
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-WEB_MERCATOR_MAX_LAT, le=WEB_MERCATOR_MAX_LAT)
    zoom: int = Field(default=14, ge=8, le=18)
    width: int = Field(default=1280, ge=256, le=2048)
    height: int = Field(default=720, ge=256, le=2048)
    profile: Profile = "walking"
    travel_times: TravelTimes = Field(default_factory=lambda: [5, 10, 15, 20])
