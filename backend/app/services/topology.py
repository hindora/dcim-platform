"""Topology service: scope resolution, traversal, truncation and caching.

The API talks about layers the way an operator does; the database stores them
as a ``layer_t`` enum that does not use quite the same words. That translation
lives here rather than leaking either vocabulary into the other.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.layers import UPSTREAM_COL
from app.core.logging import get_logger
from app.repositories import topology as repo
from app.schemas import (
    ImpactLayerOut,
    ImpactNode,
    ImpactOut,
    LocationRef,
    Termination,
    TopologyEdge,
    TopologyNode,
    TopologyOut,
    TraceHop,
    TraceNeighbour,
    TraceOut,
    TracePath,
    TraceTermination,
)
from app.services import impact
from app.services import trace as trace_svc

log = get_logger("topology")

# Cap from docs/10 section 6. Past this a browser cannot lay the graph out
# usefully anyway, and the honest answer is "narrow the scope", not a slower
# response with more nodes in it.
NODE_CAP = 2000

# How long a rendered graph may be reused. The structure changes only on
# import, but oper_state moves when a link drops, so this is deliberately short.
# Live state belongs on the websocket (4.6), not in a longer TTL here.
CACHE_TTL_S = 30

SCOPE_TYPES = {"datacenter", "room", "rack", "device"}

# API name -> layer_t value. 'network' is what operators and the API spec call
# the data plane; the enum calls it 'production'. Both are accepted so neither
# vocabulary has to win.
LAYER_ALIASES = {
    "network": "production",
    "production": "production",
    "management": "management",
    "power": "power",
    "cooling": "cooling",
    "fieldbus": "fieldbus",
}

# Metric columns on device_state worth carrying on a node. Kept to the typed
# columns rather than the metrics jsonb: a topology view wants load and inlet
# temperature at a glance, not every sample the device has ever produced.
_NODE_METRICS = ("power_w", "inlet_temp_c", "cpu_util_pct", "humidity_pct")


ROLLUPS = {"none", "rack"}

# A group has to be worth collapsing. One device behind a rack node hides its
# name and its status behind a synthetic box that says "1 server", which is
# strictly worse than the device.
_MIN_ROLLUP_GROUP = 2


class TopologyError(ValueError):
    """Bad request, carrying a message meant for the caller to read."""


def parse_scope(scope: str) -> tuple[str, str]:
    """Split ``room:<uuid>`` into its parts, rejecting anything else.

    The id is validated as a UUID here so a malformed scope is a 400 with a
    useful message rather than a 500 out of Postgres complaining about a cast.
    """
    kind, _, ident = scope.partition(":")
    kind = kind.strip().lower()
    if not ident or kind not in SCOPE_TYPES:
        raise TopologyError(
            f"scope must be one of {sorted(SCOPE_TYPES)} as '<type>:<id>', got {scope!r}")
    try:
        uuid.UUID(ident)
    except ValueError:
        raise TopologyError(f"scope id {ident!r} is not a UUID") from None
    return kind, ident


def resolve_layer(layer: str) -> str:
    key = layer.strip().lower()
    if key == "physical":
        # Not an oversight. The physical layer is rack containment and floor
        # geometry, which lives in the location tables, not in `connection`.
        # Answering with an empty graph would look like "nothing is racked".
        raise TopologyError(
            "layer 'physical' is not part of the connection graph; use the rack "
            "elevation and floor plan endpoints for containment and coordinates")
    if key not in LAYER_ALIASES:
        raise TopologyError(
            f"unknown layer {layer!r}; expected one of "
            f"{sorted(set(LAYER_ALIASES) | {'physical'})}")
    return LAYER_ALIASES[key]


def _node_from_row(row: dict[str, Any]) -> TopologyNode:
    metrics = {k: float(row[k]) for k in _NODE_METRICS
               if row.get(k) is not None}
    # Carried alongside the readings rather than as a field of its own: it is
    # the denominator of one of them, and a node that has no rating recorded
    # must render as "no limit recorded" rather than as 0 W.
    if row.get("rated_power_w"):
        metrics["rated_power_w"] = float(row["rated_power_w"])
    return TopologyNode(
        id=row["id"], name=row["name"], device_type=row["device_type"],
        status=row["status"], max_severity=row["max_severity"],
        depth=row["depth"],
        location=LocationRef(
            datacenter_id=row.get("datacenter_id"),
            datacenter_code=row.get("datacenter_code"),
            room_id=row.get("room_id"), room_name=row.get("room_name"),
            rack_id=row.get("rack_id"), rack_name=row.get("rack_name"),
        ),
        metrics=metrics,
    )


def _edges_from_rows(rows: list[dict[str, Any]],
                     labels: dict[str, str]) -> list[TopologyEdge]:
    out = []
    for r in rows:
        out.append(TopologyEdge(
            id=r["id"], source=r["source"], target=r["target"],
            layer=r["layer"], link_type=r.get("link_type"),
            redundancy_side=r.get("redundancy_side"),
            oper_state=r.get("oper_state") or "unknown",
            a_termination=Termination(
                type=r["a_termination_type"], id=r.get("a_termination_id"),
                label=labels.get(r.get("a_termination_id") or "")),
            b_termination=Termination(
                type=r["b_termination_type"], id=r.get("b_termination_id"),
                label=labels.get(r.get("b_termination_id") or "")),
        ))
    return out


async def _termination_labels(session: AsyncSession,
                              rows: list[dict[str, Any]]) -> dict[str, str]:
    by_type: dict[str, list[str]] = {}
    for r in rows:
        for side in ("a", "b"):
            ttype = r[f"{side}_termination_type"]
            tid = r.get(f"{side}_termination_id")
            if ttype and ttype != "none" and tid:
                by_type.setdefault(ttype, []).append(tid)
    return await repo.termination_labels(session, by_type)


# Worst-first, so a rolled-up rack inherits the state of its unhappiest member
# rather than an average nobody could act on.
_SEVERITY_RANK = {"CLEAR": 0, "INFO": 1, "WARNING": 2, "MINOR": 2,
                  "MAJOR": 3, "CRITICAL": 4}


def _rollup_by_rack(nodes: list[TopologyNode], edges: list[TopologyEdge],
                    layer_value: str) -> tuple[list[TopologyNode], list[TopologyEdge]]:
    """Collapse each rack's leaf equipment into one node.

    A server hall's power layer is eight hundred loads and forty feeders. Every
    one of those loads is a 132px box with a name nobody reads at that scale,
    and the shape of the distribution - which is the entire question - is
    invisible underneath them. Collapsed to one node per rack it is sixty
    boxes and the shape is the picture.

    Only LEAVES collapse: a device that feeds nothing else on this layer. A PDU
    in the same rack stays a node of its own, because the chain runs through
    it. That rule is per-layer and not per-device-type on purpose - a server is
    a leaf on power and on the fabric, but a BMC-fronted server is NOT a leaf
    on management if something hangs off it.
    """
    up_col, _ = UPSTREAM_COL[layer_value]
    up_is_a = up_col == "a_device_id"

    def ends(e: TopologyEdge) -> tuple[str, str]:
        """(upstream, downstream) for this layer's orientation."""
        return (e.source, e.target) if up_is_a else (e.target, e.source)

    feeds_something = {ends(e)[0] for e in edges}

    groups: dict[tuple[str, str], list[TopologyNode]] = {}
    for n in nodes:
        if n.location.rack_id and n.id not in feeds_something:
            groups.setdefault((n.location.rack_id, n.device_type), []).append(n)

    collapsing = {k: v for k, v in groups.items() if len(v) >= _MIN_ROLLUP_GROUP}
    if not collapsing:
        return nodes, edges

    member_of: dict[str, str] = {}
    rolled: list[TopologyNode] = []
    for (rack_id, device_type), members in collapsing.items():
        synthetic_id = f"rack:{rack_id}:{device_type}"
        for m in members:
            member_of[m.id] = synthetic_id

        offline = sum(1 for m in members if m.status == "OFFLINE")
        unknown = sum(1 for m in members if m.status == "UNKNOWN")
        status = ("OFFLINE" if offline == len(members)
                  else "UNKNOWN" if unknown == len(members) else "ONLINE")

        # Power adds up; temperature does not. A rack's draw is the sum of its
        # loads, and its inlet is the worst one in it - an average would hide
        # the single hot server that is the reason anyone is looking.
        power = sum(m.metrics["power_w"] for m in members if "power_w" in m.metrics)
        inlets = [m.metrics["inlet_temp_c"] for m in members
                  if "inlet_temp_c" in m.metrics]
        metrics: dict[str, float] = {}
        if power:
            metrics["power_w"] = power
        if inlets:
            metrics["inlet_temp_c"] = max(inlets)
        # The rating sums only if EVERY member has one. A part-rated group
        # would print a draw against a smaller denominator than it is really
        # running against, which reads as overload where there is headroom.
        rated = [m.metrics.get("rated_power_w") for m in members]
        if all(r is not None for r in rated):
            metrics["rated_power_w"] = sum(rated)  # type: ignore[arg-type]

        first = members[0]
        rolled.append(TopologyNode(
            id=synthetic_id,
            name=first.location.rack_name or "Rack",
            device_type=device_type,
            status=status,
            max_severity=max((m.max_severity for m in members),
                             key=lambda s: _SEVERITY_RANK.get(s, 0)),
            depth=min(m.depth for m in members),
            location=LocationRef(
                datacenter_id=first.location.datacenter_id,
                datacenter_code=first.location.datacenter_code,
                room_id=first.location.room_id, room_name=first.location.room_name,
                rack_id=first.location.rack_id, rack_name=first.location.rack_name,
            ),
            metrics=metrics,
            rolled_up=len(members),
            offline_count=offline,
        ))

    kept = [n for n in nodes if n.id not in member_of] + rolled

    # Re-point every edge at the node that swallowed its end, then merge the
    # duplicates that produces. Forty cords from one RPP into one rack are one
    # line carrying the number forty, not forty identical lines.
    merged: dict[tuple[str, str, str | None], TopologyEdge] = {}
    for e in edges:
        source = member_of.get(e.source, e.source)
        target = member_of.get(e.target, e.target)
        if source == target:
            # Both ends landed inside the same collapsed group. The link is
            # real but it is now internal to one box, and drawing it as a
            # self-loop says nothing.
            continue
        key = (source, target, e.redundancy_side)
        acc = merged.get(key)
        if acc is None:
            merged[key] = e.model_copy(update={
                "source": source, "target": target,
                "count": e.count,
                "down_count": e.count if e.oper_state == "down" else 0,
                # A merged edge does not land on one outlet, so claiming a
                # termination would be a lie with a label on it.
                "a_termination": (Termination() if source != e.source or target != e.target
                                  else e.a_termination),
                "b_termination": (Termination() if source != e.source or target != e.target
                                  else e.b_termination),
            })
            continue
        acc.count += e.count
        if e.oper_state == "down":
            acc.down_count += e.count
        acc.a_termination = Termination()
        acc.b_termination = Termination()
        if acc.down_count == acc.count:
            acc.oper_state = "down"
        elif acc.oper_state == "down":
            acc.oper_state = "up"

    return kept, list(merged.values())


async def get_topology(session: AsyncSession, *, layer: str, scope: str,
                       depth: int, rollup: str = "none") -> TopologyOut:
    layer_value = resolve_layer(layer)
    scope_type, scope_id = parse_scope(scope)
    if rollup not in ROLLUPS:
        raise TopologyError(
            f"rollup must be one of {sorted(ROLLUPS)}, got {rollup!r}")

    version = await repo.graph_version(session)
    cache_key = (f"dcim:topo:{version}:{layer_value}:{scope_type}:{scope_id}"
                 f":{depth}:{rollup}")

    cached = await _cache_get(cache_key)
    if cached is not None:
        return TopologyOut.model_validate(cached)

    node_rows = await repo.graph_nodes(
        session, scope_type=scope_type, scope_id=scope_id,
        layer=layer_value, depth=depth, cap=NODE_CAP)

    # total_reached is the pre-cap count, carried on every row by a window
    # function so truncation is detectable without a second counting query.
    total_reached = node_rows[0]["total_reached"] if node_rows else 0
    truncated = total_reached > len(node_rows)
    if truncated:
        log.info("topology truncated", layer=layer_value, scope=scope,
                 reached=total_reached, returned=len(node_rows))

    ids = [r["id"] for r in node_rows]
    edge_rows = await repo.graph_edges(session, layer=layer_value, device_ids=ids)
    labels = await _termination_labels(session, edge_rows)

    nodes = [_node_from_row(r) for r in node_rows]
    edges = _edges_from_rows(edge_rows, labels)
    if rollup == "rack":
        # After the cap, not before it. The cap is a bound on the QUERY, and a
        # roll-up cannot un-truncate a walk that was already cut short - so a
        # scope big enough to truncate still says so.
        nodes, edges = _rollup_by_rack(nodes, edges, layer_value)

    result = TopologyOut(
        layer=layer, scope=scope, depth=depth,
        nodes=nodes,
        edges=edges,
        truncated=truncated,
        node_count=len(nodes), edge_count=len(edges),
        device_count=len(node_rows), conductor_count=len(edge_rows),
    )
    await _cache_set(cache_key, result)
    return result


# --- cache ------------------------------------------------------------------
#
# A cache miss must never be a request failure: Redis on this deployment is
# memory-capped and has been OOM-killed before, and a topology view that breaks
# when the cache is unavailable is worse than one that is merely slower.

# One client for the process, not one per call. redis-py's async client owns a
# connection pool and is safe to share; building a fresh one per request meant
# a TCP connect and an AUTH round trip on the cache-hit path, which measured
# 89 ms - three times the cost of just running the query again, so the cache
# was making things slower.
_redis: Redis | None = None


def _client() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().redis_url)
    return _redis


async def close_cache() -> None:
    """Release the shared client. Called on application shutdown."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def _cache_get(key: str) -> dict[str, Any] | None:
    try:
        raw = await _client().get(key)
        return json.loads(raw) if raw else None
    except Exception as exc:
        log.warning("topology cache read failed; serving from the database",
                    error=str(exc))
        return None


async def _cache_set(key: str, value: TopologyOut) -> None:
    try:
        await _client().set(key, value.model_dump_json(), ex=CACHE_TTL_S)
    except Exception as exc:
        log.warning("topology cache write failed", error=str(exc))


# A feeder can serve a lot of loads. The list below the trace is "what is
# plugged into this", which stops being that at a couple of screens; past the
# cap the count is the honest answer and the diagram is the place to go.
DOWNSTREAM_CAP = 100


async def get_trace(session: AsyncSession, *, device_id: str,
                    layer: str) -> TraceOut:
    """The chain from one device back to its source, hop by hop.

    See ``services/trace`` for why this is upstream-only and what the one hop
    of downstream is for.
    """
    layer_value = resolve_layer(layer)
    try:
        uuid.UUID(device_id)
    except ValueError:
        raise TopologyError(f"device id {device_id!r} is not a UUID") from None

    subject = (await repo.devices_brief(session, [device_id])).get(device_id)
    if subject is None:
        raise TopologyError(f"no device {device_id}")

    graph = trace_svc.build(
        await repo.layer_edges_detailed(session, layer_value))
    paths, truncated = trace_svc.trace_up(graph, device_id)
    down = trace_svc.downstream(graph, device_id)

    # One lookup for every device and every termination the answer mentions,
    # rather than one per hop. A six-hop dual-fed trace touches a dozen of each
    # and the difference is twenty-four round trips.
    edges = [h.edge for p in paths for h in p.hops] + down[:DOWNSTREAM_CAP]
    device_ids = {device_id}
    for p in paths:
        for h in p.hops:
            device_ids.update(h.alternates)
    by_type: dict[str, list[str]] = {}
    for e in edges:
        device_ids.add(e.up)
        device_ids.add(e.down)
        for ttype, tid in ((e.up_termination_type, e.up_termination_id),
                           (e.down_termination_type, e.down_termination_id)):
            if ttype and ttype != "none" and tid:
                by_type.setdefault(ttype, []).append(tid)

    brief = await repo.devices_brief(session, sorted(device_ids))
    terms = await repo.termination_details(session, by_type)

    def node(dev_id: str) -> ImpactNode:
        row = brief.get(dev_id)
        # A device on an edge that the brief lookup missed would mean the
        # connection outlived its device, which the foreign key forbids - but
        # rendering an id is still better than a 500.
        return ImpactNode(**row) if row else ImpactNode(
            id=dev_id, name=dev_id, device_type="unknown")

    def term(ttype: str, tid: str | None) -> TraceTermination:
        row = terms.get(tid or "") or {}
        return TraceTermination(
            type=ttype or "none", id=tid,
            label=row.get("label"), connector=row.get("connector"),
            rated_amps=float(row["rated_amps"]) if row.get("rated_amps") is not None else None,
            phase=row.get("phase"), branch=row.get("branch"),
            rated_watts=row.get("rated_watts"), speed_bps=row.get("speed_bps"),
        )

    def hop(h: trace_svc.Hop) -> TraceHop:
        e = h.edge
        return TraceHop(
            connection_id=e.id, up=node(e.up), down=node(e.down),
            link_type=e.link_type, redundancy_side=e.redundancy_side,
            oper_state=e.oper_state,
            up_termination=term(e.up_termination_type, e.up_termination_id),
            down_termination=term(e.down_termination_type, e.down_termination_id),
            alternates=[node(a) for a in h.alternates],
        )

    out = TraceOut(
        device=ImpactNode(**subject),
        layer=layer,
        paths=[TracePath(side=p.side, verdict=p.verdict,
                         hops=[hop(h) for h in p.hops]) for p in paths],
        truncated=truncated,
        asymmetric=trace_svc.is_asymmetric(paths),
        is_source=not paths and device_id in graph.nodes,
        downstream=[
            TraceNeighbour(
                device=node(e.down), redundancy_side=e.redundancy_side,
                oper_state=e.oper_state,
                termination=term(e.down_termination_type, e.down_termination_id))
            for e in down[:DOWNSTREAM_CAP]
        ],
        downstream_count=len(down),
    )
    log.info("trace walked", device=subject["name"], layer=layer_value,
             paths=len(out.paths), truncated=truncated,
             asymmetric=out.asymmetric, downstream=out.downstream_count)
    return out


async def get_impact(session: AsyncSession, device_id: str) -> ImpactOut:
    """What would stop working if this device were removed.

    Every layer is evaluated, but only layers where the device actually has
    dependents are returned - a server appears on the power and network layers
    and has no downstream on either, and listing three empty sections says
    nothing.
    """
    try:
        uuid.UUID(device_id)
    except ValueError:
        raise TopologyError(f"device id {device_id!r} is not a UUID") from None

    subject = (await repo.devices_brief(session, [device_id])).get(device_id)
    if subject is None:
        raise TopologyError(f"no device {device_id}")

    results: list[impact.LayerImpact] = []
    for layer in impact.LAYERS:
        graph = impact.Graph()
        for e in await repo.layer_edges(session, layer):
            graph.add(e["up"], e["down"], e.get("redundancy_side"))
        if device_id not in graph.nodes:
            continue
        outcome = impact.analyse(graph, device_id, layer)
        if outcome.dependents:
            results.append(outcome)

    wanted: set[str] = set()
    for r in results:
        wanted |= r.cut_off | r.degraded
    brief = await repo.devices_brief(session, sorted(wanted))

    def nodes(ids: set[str]) -> list[ImpactNode]:
        found = [ImpactNode(**brief[i]) for i in ids if i in brief]
        return sorted(found, key=lambda n: (n.device_type, n.name))

    layers = [
        ImpactLayerOut(layer=r.layer, effect=r.effect, dependents=len(r.dependents),
                       cut_off=nodes(r.cut_off), degraded=nodes(r.degraded))
        for r in results
    ]
    all_cut = set().union(*(r.cut_off for r in results)) if results else set()
    all_deg = set().union(*(r.degraded for r in results)) if results else set()

    log.info("impact analysed", device=subject["name"],
             layers=[r.layer for r in results],
             cut_off=len(all_cut), degraded=len(all_deg))
    return ImpactOut(device=ImpactNode(**subject), layers=layers,
                     total_cut_off=len(all_cut), total_degraded=len(all_deg))
