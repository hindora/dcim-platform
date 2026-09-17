"""Pydantic request/response models.

These are the API's contract with the browser. They are deliberately separate
from the ORM: a response model that is an ORM object leaks column changes into
the public API and makes lazy-loading a latency bug.
"""

from __future__ import annotations

from datetime import date, datetime
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
    # Asset-view fields, added for the /assets workspace (docs/21 §2). Optional
    # and additive: consumers that predate the asset module ignore them, which
    # is what lets the Devices pages stay byte-identical.
    asset_tag: str | None = None
    serial_number: str | None = None
    lifecycle: str = "in_service"
    category: str | None = None
    warranty_expires: date | None = None
    #: Derived server-side: active | expiring | expired | unknown.
    warranty_state: str = "unknown"
    owner_group: str | None = None
    cost_centre: str | None = None
    tags: list[dict[str, Any]] = Field(default_factory=list)


class EndpointSummary(BaseModel):
    id: str
    protocol: str
    role: str
    address: str | None = None
    port: int | None = None
    enabled: bool = True
    admin_state: str = "enabled"
    # Protocol-specific selectors: a Modbus unit ID, a BACnet device instance.
    # For anything behind a gateway this - not the address - is what decides
    # which device answers.
    addressing: dict[str, Any] = Field(default_factory=dict)
    credential_id: str | None = None
    credential_name: str | None = None
    credential_hint: str | None = None      # never the secret itself
    poll_profile_id: str | None = None
    poll_profile_name: str | None = None
    # Set when this endpoint is reached THROUGH another one - a serial gateway
    # or a BACnet router. Its address is the gateway's and is not editable here.
    via_endpoint_id: str | None = None
    via_name: str | None = None
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


class PowerSupplyOut(BaseModel):
    """One PSU slot, as built - inventory rather than telemetry."""

    number: int
    connector: str | None = None
    rated_watts: int | None = None
    #: The outlet at the far end of this cord. Null means the supply is fitted
    #: but not corded - which is a finding, not a blank.
    feed_device_id: str | None = None
    feed_device: str | None = None
    feed_outlet: int | None = None


class DeviceDetail(DeviceSummary):
    # serial_number, asset_tag, lifecycle and category are inherited: they were
    # promoted onto DeviceSummary so the asset list can render them without a
    # per-row fetch. Re-declaring them here would shadow the parent for no gain.
    #: The model's datasheet rating, not a reading. What the chassis is built
    #: to draw at most, which is the number a rack or a feed is sized against -
    #: and the one an operator wants beside a live draw to know the headroom.
    rated_power_w: int | None = None
    psus: list[PowerSupplyOut] = Field(default_factory=list)
    u_height: int = 1
    facing: str | None = None
    admin_state: str = "enabled"
    attributes: dict[str, Any] = Field(default_factory=dict)
    endpoints: list[EndpointSummary] = Field(default_factory=list)


class Termination(BaseModel):
    """Which physical port an edge lands on.

    ``type`` is 'none' for gear cabled without port-level detail, which is a
    real state and not an error - plenty of plant is recorded as connected
    without anyone tracing the individual conductor.
    """

    type: str = "none"
    id: str | None = None
    label: str | None = None


class TopologyNode(BaseModel):
    id: str
    name: str
    device_type: str
    status: str = "UNKNOWN"
    max_severity: str = "CLEAR"
    # Hops from the scope anchor. 0 means the node was in the requested scope
    # itself rather than pulled in by traversal, which is how a client tells
    # "my room" from "the switchgear that feeds it".
    depth: int = 0
    location: LocationRef = Field(default_factory=LocationRef)
    metrics: dict[str, float] = Field(default_factory=dict)
    # How many real devices this node stands for. 0 means it IS one device;
    # anything higher means it is a rack's worth of leaf equipment collapsed
    # into one box, and the id is synthetic - it does not resolve to a device.
    rolled_up: int = 0
    offline_count: int = 0
    # The real devices behind a rolled-up node. Empty when the node IS a
    # device. Carried because anything that answers a question about DEVICES -
    # impact analysis first among them - comes back keyed by device id, and a
    # synthetic rack node has no way to tell whether the answer is about it
    # without them. A rack that is half dark is also the true answer, and only
    # the membership can say so.
    member_ids: list[str] = Field(default_factory=list)


class TopologyEdge(BaseModel):
    id: str
    source: str
    target: str
    layer: str
    # How many conductors this edge stands for. Always 1 unless a roll-up
    # merged the edges to the devices behind one collapsed node: forty cords
    # into a rack are one line, and the count is the fact worth keeping.
    count: int = 1
    down_count: int = 0
    link_type: str | None = None
    # 'A' or 'B' for power and cooling paths; None on layers where the concept
    # does not apply, and also None wherever the importer has not derived it.
    redundancy_side: str | None = None
    oper_state: str = "unknown"
    a_termination: Termination = Field(default_factory=Termination)
    b_termination: Termination = Field(default_factory=Termination)


class TopologyOut(BaseModel):
    layer: str
    scope: str
    depth: int
    nodes: list[TopologyNode] = Field(default_factory=list)
    edges: list[TopologyEdge] = Field(default_factory=list)
    # True when the node cap was hit. The client should narrow the scope
    # rather than assume it is looking at the whole graph.
    truncated: bool = False
    # What was RETURNED - the boxes and lines the client will draw.
    node_count: int = 0
    edge_count: int = 0
    # What those stand for. Equal to the counts above with rollup=none; under
    # a roll-up they are the real devices and real conductors behind them, and
    # the difference is what the view has to admit it is hiding.
    device_count: int = 0
    conductor_count: int = 0
    # Devices in the scope with NO connection on this layer. They are not in
    # this layer's graph, so they are not drawn - a switch has nothing to do
    # with the chilled water and floats above a cooling diagram as a box with
    # no lines. The count stays, because "nine things in this room are on no
    # cooling circuit anyone recorded" is a finding rather than a blank.
    unconnected_count: int = 0


class RoomExtent(BaseModel):
    width_m: float = 0.0
    depth_m: float = 0.0
    # True when the outline was inferred from equipment positions because the
    # source carries no room dimensions. The UI says so rather than drawing a
    # wall the data does not support.
    derived: bool = True


class FloorRack(BaseModel):
    id: str
    name: str
    row_name: str | None = None
    x: float
    y: float
    # 'N' faces lower y, 'S' faces higher y - the rack's orientation in the
    # hall, which is what makes an aisle cold or hot.
    facing: str | None = None
    device_count: int = 0
    offline_count: int = 0
    load_kw: float | None = None
    max_inlet_c: float | None = None
    max_severity: str = "CLEAR"
    free_u: int | None = None


class FloorEquipment(BaseModel):
    """Plant in the room, listed rather than placed.

    It carries no x/y because the source has none for it: the only position it
    has is a pixel coordinate in a fleet-wide canvas diagram, and drawing that
    on a metre-scale room plan puts a CRAH kilometres outside its own room.
    """

    id: str
    name: str
    device_type: str
    status: str = "UNKNOWN"
    max_severity: str = "CLEAR"
    power_w: float | None = None


class FloorAisle(BaseModel):
    y_start: float
    y_end: float
    # cold | hot | unknown. Unknown is honest: a row whose racks disagree on
    # orientation has no single front, and a mislabelled aisle sends someone
    # looking for a hot spot on the wrong side of a row.
    kind: str
    label: str | None = None
    rows: list[str] = Field(default_factory=list)


class FloorPlan(BaseModel):
    room_id: str
    room_name: str
    datacenter_code: str | None = None
    extent: RoomExtent
    # Assumed standard cabinet footprint in metres; not stored per rack.
    rack_w_m: float = 0.6
    rack_d_m: float = 1.2
    racks: list[FloorRack] = Field(default_factory=list)
    # In the room, but with no coordinate to draw it at.
    unpositioned_equipment: list[FloorEquipment] = Field(default_factory=list)
    aisles: list[FloorAisle] = Field(default_factory=list)


class ImpactNode(BaseModel):
    id: str
    name: str
    device_type: str
    status: str = "UNKNOWN"
    room_name: str | None = None
    rack_name: str | None = None


class ImpactLayerOut(BaseModel):
    layer: str
    # Plain words, because "cut_off" means something different per layer:
    # losing power is not the same class of event as losing monitoring.
    effect: str
    dependents: int = 0
    # Devices with no surviving path from any source once the candidate is
    # removed. On the power layer this is the answer to "what goes dark".
    cut_off: list[ImpactNode] = Field(default_factory=list)
    # Still served, but by fewer distinct redundancy sides than before. Power
    # only - no other layer here has a labelled second path.
    degraded: list[ImpactNode] = Field(default_factory=list)


class ImpactOut(BaseModel):
    device: ImpactNode
    layers: list[ImpactLayerOut] = Field(default_factory=list)
    # Totals across layers, deduplicated by device.
    total_cut_off: int = 0
    total_degraded: int = 0


class TraceTermination(BaseModel):
    """A termination as a trace row prints it.

    Wider than ``Termination`` on purpose. A trace is read by someone holding
    a cord: the label says which socket, and the connector and rating say
    whether the cord fits it and what trips if it does not. Every electrical
    field is optional because plenty of gear is recorded as connected without
    anyone having traced the individual conductor, and an absent rating is a
    real state - it is not zero amps.
    """

    type: str = "none"
    id: str | None = None
    label: str | None = None
    connector: str | None = None
    rated_amps: float | None = None
    phase: str | None = None
    branch: str | None = None
    rated_watts: int | None = None
    speed_bps: int | None = None


class TraceHop(BaseModel):
    """One conductor, oriented source-ward: ``up`` feeds ``down``."""

    connection_id: str
    up: ImpactNode
    down: ImpactNode
    link_type: str | None = None
    redundancy_side: str | None = None
    oper_state: str = "unknown"
    up_termination: TraceTermination = Field(default_factory=TraceTermination)
    down_termination: TraceTermination = Field(default_factory=TraceTermination)
    # The other devices feeding ``down`` at this point - a generator beside a
    # utility on a switchgear board. The walk took one and names the rest here
    # rather than multiplying the chain out once per combination.
    alternates: list[ImpactNode] = Field(default_factory=list)


class TracePath(BaseModel):
    """One CORD's chain to a source, ordered source first.

    One per cord, not one per route. A dual-corded load has two chains however
    many ways the switchgear above it can be fed; the ways are ``alternates``
    on the hop where they occur.
    """

    side: str | None = None
    # 'complete' reached a device nothing feeds. 'incomplete' closed on itself
    # or hit the hop bound before it got there, which is a fact about the
    # recorded graph rather than about the wiring.
    verdict: str = "complete"
    hops: list[TraceHop] = Field(default_factory=list)


class TraceNeighbour(BaseModel):
    """One hop down: something plugged into the traced device."""

    device: ImpactNode
    redundancy_side: str | None = None
    oper_state: str = "unknown"
    termination: TraceTermination = Field(default_factory=TraceTermination)


class TraceOut(BaseModel):
    device: ImpactNode
    layer: str
    paths: list[TracePath] = Field(default_factory=list)
    # True when the path enumeration hit its bound. Real estates do not reach
    # it; a graph that does has a shape worth looking at rather than a longer
    # list worth printing.
    truncated: bool = False
    # The labelled sides disagree about how far away the source is. Not
    # automatically wrong, but it is the signature of a B side extended during
    # a build and never squared up.
    asymmetric: bool = False
    # Nothing feeds the traced device on this layer: it IS a source here.
    # Distinct from "no paths were found", which would be a bug.
    is_source: bool = False
    downstream: list[TraceNeighbour] = Field(default_factory=list)
    downstream_count: int = 0


class RedundancyFinding(BaseModel):
    device: ImpactNode
    # single_fed | same_side | converged. Named rather than graded: whether a
    # single-corded sensor is a defect is the operator's call, not the
    # service's, and burying that decision in a threshold here would make the
    # audit unreadable at every other site.
    kind: str
    # One sentence saying what the finding means, so a reader who has not met
    # the vocabulary still gets the point.
    detail: str
    # For `converged`: the devices BOTH sides pass through - the common mode.
    # Nearest the load first, because the closer a convergence is the more of
    # the chain it takes with it.
    shared: list[ImpactNode] = Field(default_factory=list)
    sides: list[str] = Field(default_factory=list)


class RedundancyOut(BaseModel):
    layer: str
    scope: str
    # Devices examined: those in scope with at least one feed on this layer.
    examined: int = 0
    findings: list[RedundancyFinding] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class PathOut(BaseModel):
    layer: str
    src: ImpactNode
    dst: ImpactNode
    # The shortest walk between them, ignoring flow direction: on a power
    # layer two loads are both leaves, so any connection runs UP from one and
    # back DOWN to the other and there is no directed path at all.
    hops: list[ImpactNode] = Field(default_factory=list)
    connected: bool = False
    # The real answer to "are these two independent". Two racks on separate
    # feeders can still be one breaker away from each other, and a walk
    # between them says nothing about that - what says it is what they both
    # hang off. Nearest-first, so the closest common point is the one named.
    shared_upstream: list[ImpactNode] = Field(default_factory=list)
    # No equipment upstream of both. On a directed layer this is the whole
    # question; on ethernet there is no upstream and it is always false.
    independent: bool = False


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
    mac: str | None = None
    admin_state: str = "enabled"
    #: The far end of the cable on this port, when there is one. A port with no
    #: peer is a spare - which is a fact worth showing, not an absence to hide.
    peer_device_id: str | None = None
    peer_device: str | None = None
    peer_port: str | None = None
    #: Which plane the cable belongs to - production, management. A server's
    #: BMC port and its data NICs are different networks that fail separately.
    peer_layer: str | None = None


# ------------------------------------------------------------------- racks

class RackSummary(BaseModel):
    id: str
    name: str
    row_name: str | None = None
    # Order of the row in the room and of the rack in its row - what a
    # name like "R2-04" encodes, spelled out for the page header.
    row_ordinal: int | None = None
    position: int | None = None
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
