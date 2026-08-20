"""Pydantic request/response models.

These are the API's contract with the browser. They are deliberately separate
from the ORM: a response model that is an ORM object leaks column changes into
the public API and makes lazy-loading a latency bug.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    total: int | None = None


# ------------------------------------------------------------------ common

class LocationRef(BaseModel):
    datacenter_id: str | None = None
    datacenter_code: str | None = None
    room_id: str | None = None
    room_name: str | None = None
    row_name: str | None = None
    rack_id: str | None = None
    rack_name: str | None = None
    u_start: int | None = None


class MetricValue(BaseModel):
    v: float | bool | str | None = None
    u: str | None = None
    t: datetime | None = None
    q: str = "good"


# ----------------------------------------------------------------- devices

class DeviceSummary(BaseModel):
    id: str
    name: str
    device_type: str
    vendor: str | None = None
    model: str | None = None
    status: str = "UNKNOWN"
    health: str = "UNKNOWN"
    max_severity: str = "CLEAR"
    mgmt_ip: str | None = None
    primary_ip: str | None = None
    last_seen: datetime | None = None
    location: LocationRef = Field(default_factory=LocationRef)


class EndpointSummary(BaseModel):
    id: str
    protocol: str
    role: str
    address: str | None = None
    port: int | None = None
    enabled: bool = True
    credential_hint: str | None = None      # never the secret itself
    poll_interval_s: int | None = None
    status: str = "UNKNOWN"
    # last_seen is every poll ATTEMPT, last_success only the ones that worked.
    # Fresh last_seen with a stale last_success is an endpoint being polled and
    # failing, which reads very differently from one nothing is polling at all.
    last_seen: datetime | None = None
    last_success: datetime | None = None
    last_error: str | None = None
    last_error_class: str | None = None
    consecutive_failures: int = 0
    last_latency_ms: int | None = None
    # Lifetime totals, not a recent window; see the repository query.
    poll_count: int = 0
    fail_count: int = 0
    timeout_count: int = 0
    auth_fail_count: int = 0


class DeviceDetail(DeviceSummary):
    serial_number: str | None = None
    asset_tag: str | None = None
    u_height: int = 1
    facing: str | None = None
    lifecycle: str = "in_service"
    admin_state: str = "enabled"
    attributes: dict[str, Any] = Field(default_factory=dict)
    endpoints: list[EndpointSummary] = Field(default_factory=list)


class DeviceStateOut(BaseModel):
    device_id: str
    status: str
    health: str
    max_severity: str
    active_alarms: int = 0
    last_seen: datetime | None = None
    metrics: dict[str, MetricValue] = Field(default_factory=dict)


class InterfaceOut(BaseModel):
    id: str
    if_index: int | None = None
    name: str
    role: str
    speed_bps: int | None = None
    ip: str | None = None
    admin_state: str = "enabled"


# ------------------------------------------------------------------- racks

class RackSummary(BaseModel):
    id: str
    name: str
    row_name: str | None = None
    room_id: str | None = None
    room_name: str | None = None
    datacenter_code: str | None = None
    u_height: int = 42
    device_count: int = 0
    online_count: int = 0
    offline_count: int = 0
    load_kw: float | None = None
    rated_power_kw: float | None = None
    load_pct: float | None = None
    max_inlet_c: float | None = None
    max_severity: str = "CLEAR"
    free_u: int | None = None


class ElevationDevice(BaseModel):
    id: str
    name: str
    device_type: str
    status: str
    health: str
    max_severity: str
    power_w: float | None = None
    inlet_temp_c: float | None = None
    cpu_util_pct: float | None = None


class ElevationSlot(BaseModel):
    u_start: int
    u_height: int
    facing: str | None = None
    free: bool = False
    device: ElevationDevice | None = None


class FreeBlock(BaseModel):
    u_start: int
    u_height: int


class RackElevation(BaseModel):
    rack: RackSummary
    positions: list[ElevationSlot]
    free_blocks: list[FreeBlock]
    # Devices in the rack that occupy no rack unit: vertically mounted PDUs,
    # rail-strapped probes. Rendering them inside the U grid would imply a
    # position they do not have.
    zero_u_devices: list[ElevationDevice] = Field(default_factory=list)


# --------------------------------------------------------------- telemetry

class Series(BaseModel):
    metric: str
    instance: str = ""
    unit: str
    # [epoch_ms, value] pairs: ~4x smaller than objects and the shape every
    # charting library wants.
    points: list[list[float]]


class HistoryOut(BaseModel):
    device_id: str
    interval: str
    source: str
    series: list[Series]
    truncated: bool = False


# --------------------------------------------------------------- dashboard

class DashboardCounts(BaseModel):
    total: int = 0
    online: int = 0
    offline: int = 0
    degraded: int = 0
    unknown: int = 0


class CollectorStatusOut(BaseModel):
    id: str
    status: str
    version: str | None = None
    hostname: str | None = None
    endpoints_owned: int = 0
    endpoints_online: int = 0
    last_heartbeat: datetime | None = None
    stats: dict[str, Any] = Field(default_factory=dict)


class DashboardSummary(BaseModel):
    devices: DashboardCounts
    power: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)
    collectors: list[CollectorStatusOut] = Field(default_factory=list)
    ingest: dict[str, Any] = Field(default_factory=dict)
    as_of: datetime


# --------------------------------------------------------------- collector

class AssignmentCredential(BaseModel):
    kind: str
    # Decrypted, and returned ONLY on the collector-scoped endpoint over TLS.
    # See docs/13 section B1 for why this is unavoidable and how it is mitigated.
    data: dict[str, Any]


class AssignmentPoll(BaseModel):
    interval_s: int
    timeout_ms: int
    retries: int
    metric_groups: list[str] = Field(default_factory=list)
    push_enabled: bool = False


class AssignmentEndpoint(BaseModel):
    id: str
    device_id: str
    device_name: str
    device_type: str
    vendor: str | None = None
    model: str | None = None
    protocol: str
    role: str
    address: str | None = None
    port: int | None = None
    addressing: dict[str, Any] = Field(default_factory=dict)
    via_endpoint_id: str | None = None
    credential: AssignmentCredential | None = None
    poll: AssignmentPoll


class Assignment(BaseModel):
    version: int
    generated_at: datetime
    collector_id: str
    endpoints: list[AssignmentEndpoint]


# -------------------------------------------------------------------- auth

class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    token: str
    expires_in: int
    username: str
    role: str
