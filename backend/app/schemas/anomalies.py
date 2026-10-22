"""Response models for /api/v1/anomalies (docs/30).

Phase 0 serves only GET /anomalies/health. The shape is doc 31's
`AnomalyHealth` (D6) plus doc 33's two switch states, with the operational
figures under `detail` (docs/30 section 11).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SweepDetail(BaseModel):
    p95_ms_1h: int | None = None
    runs_1h: int = 0
    #: Should sit near runs_1h x (replicas - 1): the cadence gate at work.
    gate_skips_1h: int = 0
    errors_1h: int = 0
    last_error: str | None = None
    series_scored: int = 0


class BaselineDetail(BaseModel):
    last_fit_duration_s: float | None = None
    series_fitted: int = 0
    rejected: int = 0


class FindingsDetail(BaseModel):
    open: int = 0
    opened_24h: int = 0
    by_lane: dict[Literal["advisory", "alert"], int] = Field(
        default_factory=lambda: {"advisory": 0, "alert": 0})
    by_kind: dict[Literal["sensor", "process", "undetermined"], int] = Field(
        default_factory=lambda: {"sensor": 0, "process": 0, "undetermined": 0})


class IncidentsDetail(BaseModel):
    open: int = 0


class ExplainDetail(BaseModel):
    runs_1h: int = 0
    fallbacks_1h: int = 0


class HealthDetail(BaseModel):
    sweep: SweepDetail = Field(default_factory=SweepDetail)
    baselines: BaselineDetail = Field(default_factory=BaselineDetail)
    findings: FindingsDetail = Field(default_factory=FindingsDetail)
    incidents: IncidentsDetail = Field(default_factory=IncidentsDetail)
    explain: ExplainDetail = Field(default_factory=ExplainDetail)


class AnomalyHealth(BaseModel):
    enabled: bool
    explainer: Literal["none", "cloud", "local"]
    sweep_s: int
    last_sweep_at: datetime | None = None
    last_sweep_ms: int | None = None
    findings_per_hour: int = 0
    #: The advisory budget (docs/33 section 7). Null: no budget is set.
    budget_per_hour: int | None = None
    #: The heartbeat's consumer, e.g. 'ingest-1:31'.
    lock_holder: str | None = None
    baseline_last_fit_at: datetime | None = None
    severity_cap: str
    l5_major_enabled: bool = False
    detail: HealthDetail = Field(default_factory=HealthDetail)


# --- Phase 1 request bodies (docs/30 sections 4.3, 6.2-6.4) -------------------

class FeedbackRequest(BaseModel):
    model_config = {"extra": "forbid"}

    action: Literal["acknowledge", "dismiss", "confirm", "reopen"]
    #: Required for dismiss, refused otherwise (docs/31 D5): dismissal is how
    #: precision is measured, and free text cannot be counted.
    reason: Literal["not_a_fault", "expected_operation", "sensor_fault", "duplicate"] | None = None
    note: str | None = Field(None, max_length=1000)


class DetectorPatch(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool | None = None
    #: Partial; validated against the detector's param_schema.
    params: dict[str, Any] | None = None


class OverridePut(BaseModel):
    model_config = {"extra": "forbid"}

    params: dict[str, Any] | None = None
    enabled: bool | None = None
    reason: str | None = Field(None, max_length=1000)
    expires_at: datetime | None = None


class ExclusionCreate(BaseModel):
    model_config = {"extra": "forbid"}

    device_id: str | None = None
    room_id: str | None = None
    datacenter_id: str | None = None
    detector_id: str | None = None
    metric_key: str | None = None
    starts_at: datetime
    ends_at: datetime | None = None
    source: str = "manual"
    source_ref: str | None = Field(None, max_length=200)
    reason: str = Field(..., min_length=1, max_length=1000)
