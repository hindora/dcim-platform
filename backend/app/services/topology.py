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
from app.core.logging import get_logger
from app.repositories import topology as repo
from app.schemas import LocationRef, Termination, TopologyEdge, TopologyNode, TopologyOut

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


async def get_topology(session: AsyncSession, *, layer: str, scope: str,
                       depth: int) -> TopologyOut:
    layer_value = resolve_layer(layer)
    scope_type, scope_id = parse_scope(scope)

    version = await repo.graph_version(session)
    cache_key = f"dcim:topo:{version}:{layer_value}:{scope_type}:{scope_id}:{depth}"

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

    result = TopologyOut(
        layer=layer, scope=scope, depth=depth,
        nodes=[_node_from_row(r) for r in node_rows],
        edges=_edges_from_rows(edge_rows, labels),
        truncated=truncated,
        node_count=len(node_rows), edge_count=len(edge_rows),
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
