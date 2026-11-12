from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_PARTICIPANTS = 8

_STRICT = ConfigDict(extra="forbid", allow_inf_nan=False)

Mode = Literal["walking", "cycling", "driving", "driving-traffic"]
Objective = Literal["minimax", "sum", "hybrid"]

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

    @model_validator(mode="after")
    def check_participants(self) -> "SolveRequest":
        ids = [p.id for p in self.participants]
        if len(set(ids)) != len(ids):
            raise ValueError("Participant ids must be unique.")
        return self

class CandidateResult(BaseModel):
    # Where to meet: the point on the street network the times were measured to.
    lon: float
    lat: float
    # The street there, when the map service names one.
    name: Optional[str] = None
    # How far that point is from the grid point that was searched (metres).
    snap_m: Optional[float] = None
    score: float
    max_time_min: float
    mean_time_min: float
    times: dict[str, float]

class SolveResponse(BaseModel):
    objective: str
    best: CandidateResult
    top: list[CandidateResult]
    meeting_area_geojson: Optional[dict]
    diagnostics: dict
    # Plain sentences for the person reading the answer: why it may be less good than usual.
    notes: list[str] = Field(default_factory=list)
