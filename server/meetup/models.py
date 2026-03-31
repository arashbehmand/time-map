from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator

Mode = Literal["walking", "cycling", "driving", "driving-traffic"]
Objective = Literal["minimax", "sum", "hybrid"]

class Participant(BaseModel):
    id: str = Field(..., min_length=1)
    lon: float = Field(..., ge=-180, le=180)
    lat: float = Field(..., ge=-90, le=90)
    mode: Mode
    weight: float = Field(default=1.0, gt=0)
    max_minutes: Optional[float] = Field(default=None, gt=0)

class SearchConfig(BaseModel):
    margin_km: float = Field(default=3.0, gt=0,
        description="Extra radius beyond participant cluster (km).")
    max_candidates: int = Field(default=160, ge=20, le=500)
    h3_resolution: Optional[int] = Field(default=None, ge=6, le=11)
    refine_top: int = Field(default=6, ge=1, le=20,
        description="How many coarse winners to refine.")
    neighbor_ring: int = Field(default=1, ge=0, le=2)
    area_slack: float = Field(default=0.15, ge=0, le=1.0,
        description="Meeting area includes cells with score <= best*(1+slack).")
    top_k: int = Field(default=5, ge=1, le=30)

class SolveRequest(BaseModel):
    participants: list[Participant]
    objective: Objective = "hybrid"
    alpha: float = Field(default=0.65, ge=0, le=1.0,
        description="For hybrid: score = alpha*max + (1-alpha)*weighted_mean")
    search: SearchConfig = Field(default_factory=SearchConfig)

    @model_validator(mode="after")
    def check_participants(self) -> "SolveRequest":
        if len(self.participants) < 2:
            raise ValueError("Need at least 2 participants.")
        ids = [p.id for p in self.participants]
        if len(set(ids)) != len(ids):
            raise ValueError("Participant ids must be unique.")
        return self

class CandidateResult(BaseModel):
    lon: float
    lat: float
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
