from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_PARTICIPANTS = 8

_STRICT = ConfigDict(extra="forbid", allow_inf_nan=False)

# "transit" is the London tables (server/transit/provider.py): a group with anyone on it
# meets at a station (server/meetup/stations.py); every other group keeps the grid solver.
Mode = Literal["walking", "cycling", "driving", "driving-traffic", "transit"]
Objective = Literal["minimax", "sum", "hybrid"]
# The tables' timetable windows: weekday 08:00, 13:00, 19:00 and Saturday 13:00.
Window = Literal["wd08", "wd13", "wd19", "sa13"]
DEFAULT_WINDOW = "wd19"
# Public transport only: where to meet. A station (the default), or a named venue of one kind
# from OpenStreetMap within a 10-minute walk of a station (server/transit/venues.py, #19).
Venue = Literal["station", "pub", "bar", "cafe", "restaurant", "park"]

class Participant(BaseModel):
    model_config = _STRICT
    id: str = Field(..., min_length=1, max_length=64)
    lon: float = Field(..., ge=-180, le=180)
    lat: float = Field(..., ge=-90, le=90)
    mode: Mode
    weight: float = Field(default=1.0, gt=0, le=100)
    max_minutes: Optional[float] = Field(default=None, gt=0, le=240)

class SearchConfig(BaseModel):
    model_config = _STRICT
    margin_km: float = Field(default=3.0, gt=0, le=10,
        description="Extra radius beyond participant cluster (km).")
    max_candidates: int = Field(default=160, ge=20, le=300,
        description="Upper bound on candidates scored in each of the coarse and fine stages.")
    refine_top: int = Field(default=6, ge=1, le=10,
        description="How many coarse winners to refine.")
    neighbor_ring: int = Field(default=1, ge=0, le=1)
    area_slack: float = Field(default=0.15, ge=0, le=1.0,
        description="Meeting area includes cells with score <= best*(1+slack).")
    top_k: int = Field(default=5, ge=1, le=30)

class SolveRequest(BaseModel):
    model_config = _STRICT
    participants: list[Participant] = Field(min_length=2, max_length=MAX_PARTICIPANTS)
    objective: Objective = "hybrid"
    alpha: float = Field(default=0.65, ge=0, le=1.0,
        description="For hybrid: score = alpha*max + (1-alpha)*weighted_mean")
    search: SearchConfig = Field(default_factory=SearchConfig)
    window: Optional[Window] = Field(default=None,
        description="Public transport only: which timetable window (default weekday evening).")
    venue: Venue = Field(default="station",
        description="Public transport only: meet at a station, or at a pub, bar, café, restaurant or park.")

    @model_validator(mode="after")
    def check_participants(self) -> "SolveRequest":
        ids = [p.id for p in self.participants]
        if len(set(ids)) != len(ids):
            raise ValueError("Participant ids must be unique.")
        return self

class CandidateResult(BaseModel):
    # Where to meet: the point on the street network the times were measured to (for a
    # mixed group, the walking point; every mode's own point is within 150 m of it).
    lon: float
    lat: float
    # The street there, when the map service names one.
    name: Optional[str] = None
    # How far this point is from the grid point that was searched (metres).
    snap_m: Optional[float] = None
    score: float
    max_time_min: float
    mean_time_min: float
    times: dict[str, float]
    # "station" (a named place from the tables), a venue kind (a named place from
    # OpenStreetMap) or "street" (a grid point on a named street).
    kind: Optional[Literal["station", "street", "pub", "bar", "cafe", "restaurant", "park"]] = None
    # Stations: the place's id within the tables' build. Venues: the OSM id (node/123).
    place_id: Optional[str] = None
    # Stations: closer to the best than the tables can tell apart.
    tie: bool = False

class SolveResponse(BaseModel):
    objective: str
    best: CandidateResult
    top: list[CandidateResult]
    meeting_area_geojson: Optional[dict]
    diagnostics: dict
    # Plain sentences for the person reading the answer: why it may be less good than usual.
    notes: list[str] = Field(default_factory=list)
    # Fairest answers (stations, venues and streets): the place with the lowest average trip,
    # when it is a different place.
    quickest: Optional[CandidateResult] = None
    # Stations: the timetable window the public transport times are for.
    window: Optional[str] = None
    window_label: Optional[str] = None
    # Public transport: the date the timetable's times are for (YYYY-MM-DD), and the day after
    # which the pages say services may have changed (advisory: the tables never expire).
    service_date: Optional[str] = None
    valid_until: Optional[str] = None
    # Stations: the data credits the tables' licences ask for.
    credits: list[str] = Field(default_factory=list)
    # Public transport: the kind of place asked for, and, when the answer is a station instead,
    # why: "not_loaded", "none_in_reach", "none_work" or "slower"
    # (server/meetup/stations.py note_venue_fallback).
    venue: Optional[str] = None
    venue_fallback: Optional[str] = None
