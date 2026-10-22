"""Anomaly detection API (docs/30): findings, feedback, detectors, overrides,
exclusions and health. Incidents are Phase 2.

Roles (docs/30 section 2): every GET is viewer; finding feedback, overrides
and exclusions are operator; a detector PATCH - which changes estate policy,
including the L5 row's `major_enabled` switch - is admin.

Feature-off is not an error (docs/30 section 1). With DCIM_ANOMALY_ENABLED
false the finding reads return empty pages, /health says `enabled: false`,
and the CONFIGURATION surface (detectors, overrides, exclusions) is served
and writable, so an operator can prepare the catalogue before the sweep is
switched on (docs/29 section 11: "the API serves the catalogue").

Every 4xx carries `detail = {"error": <stable key>, "message": ..., ...}`
(docs/30 section 12). Every write is audited in the same transaction, and
websocket frames go out after commit.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.alarms.service import AlarmAction
from app.analytics import catalogue as cat
from app.analytics import sweep as anomaly_sweep
from app.analytics.catalogue import DERIVED_SEVERITY_CAP
from app.core import audit
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.metrics_gen import METRICS
from app.core.security import Principal, require_role
from app.db.session import get_session
from app.ingest.fanout import Fanout
from app.integrations import outbox as integration_outbox
from app.repositories import alarms as alarm_repo
from app.repositories import anomalies as repo
from app.schemas.anomalies import (
    AnomalyHealth,
    BaselineDetail,
    DetectorPatch,
    ExclusionCreate,
    FeedbackRequest,
    FindingsDetail,
    HealthDetail,
    IncidentsDetail,
    OverridePut,
    SweepDetail,
)

log = get_logger("api.anomalies")

router = APIRouter(prefix="/anomalies", tags=["anomalies"])

CATALOGUE_VERSION_KEY = "dcim:anomaly:catalogue:version"
SYSTEM_SOURCES = ("maintenance", "confirmed_finding")
POSTABLE_SOURCES = ("manual", "injected_fault")
EXCLUSION_SOURCES = ("maintenance", "injected_fault", "confirmed_finding", "manual")


async def get_redis(settings: Settings = Depends(get_settings)) -> AsyncIterator[Redis]:
    """One client per request, as the other Redis-reading routes do."""
    redis = Redis.from_url(settings.redis_url)
    try:
        yield redis
    finally:
        await redis.aclose()


def _fail(code: int, error: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(code, {"error": error, "message": message, **extra})


def _check_enum(values: list[str] | None, allowed: tuple[str, ...], what: str) -> None:
    bad = sorted(set(values or []) - set(allowed))
    if bad:
        raise _fail(status.HTTP_400_BAD_REQUEST, "unknown_enum",
                    f"unknown {what}: {', '.join(bad)}", field=what,
                    allowed=list(allowed))


def _check_uuid(value: str | None, what: str) -> None:
    if value is None:
        return
    try:
        uuid.UUID(value)
    except ValueError:
        raise _fail(status.HTTP_400_BAD_REQUEST, "unknown_enum",
                    f"{what} must be a uuid", field=what) from None


async def _after_commit(redis: Redis, *, frames: list[dict] | None = None,
                        actions: list | None = None) -> None:
    """Publish after commit, never before: a frame for a row that rolls back
    is the dashboard lying (ingest/fanout.py)."""
    fan = Fanout(redis)
    for a in actions or ():
        await fan.alarm(a.kind, a.alarm)
    if frames:
        await fan.anomaly_frames(frames)


async def _bump_catalogue(redis: Redis) -> None:
    try:
        await redis.incr(CATALOGUE_VERSION_KEY)
    except Exception as exc:  # the sweep reloads on its 60 s refresh anyway
        log.warning("catalogue version bump failed", error=str(exc))


# =============================================================================
# findings
# =============================================================================

def _empty_facets() -> dict[str, Any]:
    return {"open_incidents": 0, "open_findings": 0, "open_sensor": 0, "new_24h": 0,
            "by_state": dict.fromkeys(repo.FINDING_STATES, 0),
            "by_lane": dict.fromkeys(repo.LANES, 0),
            "by_kind": dict.fromkeys(repo.KINDS, 0)}


@router.get("/findings", summary="Findings: the advisory queue and the record behind "
                                 "every derived alert")
async def list_findings(
    state: list[str] | None = Query(None, description="Default: open, acknowledged, "
                                                      "confirmed"),
    lane: list[str] | None = Query(None),
    kind: list[str] | None = Query(None),
    detector: list[str] | None = Query(None),
    device_id: str | None = None,
    datacenter: str | None = Query(None, description="uuid or code, e.g. DC1"),
    room: str | None = None,
    since: datetime | None = Query(None, description="last_seen >= since"),
    alarm_id: str | None = Query(None, description="the finding behind a derived alarm"),
    incident_id: str | None = None,
    stale: bool | None = None,
    with_facets: bool = False,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    with_total: bool = False,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(require_role("viewer")),
) -> dict[str, Any]:
    _check_enum(state, repo.FINDING_STATES, "state")
    _check_enum(lane, repo.LANES, "lane")
    _check_enum(kind, repo.KINDS, "kind")
    for v, what in ((device_id, "device_id"), (room, "room"), (alarm_id, "alarm_id"),
                    (incident_id, "incident_id")):
        _check_uuid(v, what)
    if not settings.anomaly_enabled:
        out: dict[str, Any] = {"items": [], "next_cursor": None,
                               "total": 0 if with_total else None}
        if with_facets:
            out["facets"] = _empty_facets()
        return out
    return await repo.list_findings(
        session, states=state or repo.DEFAULT_STATES, lanes=lane, kinds=kind,
        detectors=detector, device_id=device_id, datacenter=datacenter, room=room,
        since=since, alarm_id=alarm_id, incident_id=incident_id, stale=stale, limit=limit,
        offset=offset, with_total=with_total, with_facets=with_facets)


async def _finding_detail(session: AsyncSession, row: dict[str, Any]) -> dict[str, Any]:
    catalogue = await repo.load_catalogue(session, datetime.now(UTC))
    det = catalogue.get(row["detector_id"])
    params: dict[str, Any] = {}
    if det is not None:
        ov = catalogue.overrides.get((det.id, row["device_id"]))
        params = cat.merge_params(det.params, ov.params if ov else None)
    evidence = row.pop("evidence", None) or {}
    feedback = row.pop("feedback", None) or []
    limit = None
    if row["layer"] == "L2":
        if evidence.get("upper") is not None:
            limit = {"value": evidence["upper"], "label": "rule limit (upper)"}
        elif evidence.get("lower") is not None:
            limit = {"value": evidence["lower"], "label": "rule limit (lower)"}
    return {**row,
            "detector": {"id": row["detector_id"], "name": row["detector_name"],
                         "version": det.version if det else row["detector_version"],
                         "layer": row["layer"], "params": params},
            "evidence": evidence, "rule_limit": limit,
            "feedback": [{"reason": None, "note": None, "via": "finding", **f}
                         for f in feedback]}


@router.get("/findings/{finding_id}", summary="One finding, with its evidence")
async def get_finding(finding_id: str, session: AsyncSession = Depends(get_session),
                      settings: Settings = Depends(get_settings),
                      _: Principal = Depends(require_role("viewer"))) -> dict[str, Any]:
    row = await repo.get_finding(session, finding_id) if settings.anomaly_enabled else None
    if row is None:
        raise _fail(404, "not_found", "no such finding")
    return await _finding_detail(session, row)


_CHANGE = {"acknowledge": "acknowledged", "dismiss": "dismissed", "confirm": "confirmed",
           "reopen": "reopened"}


@router.post("/findings/{finding_id}/feedback",
             summary="Acknowledge, dismiss (with a reason), confirm or reopen")
async def finding_feedback(
    finding_id: str, body: FeedbackRequest, request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    if body.action == "dismiss" and not body.reason:
        raise _fail(422, "reason_required",
                    "dismiss needs a reason: " + ", ".join(repo.DISMISS_REASONS),
                    allowed=list(repo.DISMISS_REASONS))
    if body.action != "dismiss" and body.reason:
        raise _fail(422, "reason_not_allowed", "a reason is given only with dismiss")
    current = await repo.finding_state(session, finding_id) \
        if await repo.get_finding(session, finding_id) else None
    if current is None:
        raise _fail(404, "not_found", "no such finding")
    actor = audit.actor_of(principal)
    done = await repo.apply_feedback(session, finding_id, action=body.action, actor=actor,
                                     reason=body.reason, note=body.note)
    if done is None:
        state = await repo.finding_state(session, finding_id)
        raise _fail(409, "illegal_transition",
                    f"cannot {body.action} a finding that is {state}", state=state,
                    allowed=repo.allowed_actions(state or ""))
    ip, agent = audit.client_of(request)
    actions = []
    # One acknowledgement path (docs/30 section 4.4): the alarm is
    # authoritative, and acknowledging an alert-lane finding acknowledges its
    # alarm in this same transaction. An alarm already acknowledged or
    # cleared is left alone, and is not an error here.
    if body.action == "acknowledge" and done["lane"] == "alert" and done["alarm_id"]:
        acked = await alarm_repo.acknowledge(session, done["alarm_id"], actor, body.note)
        if acked is not None:
            await alarm_repo.record_history(
                session, alarm_id=acked["id"], device_id=acked["device_id"],
                action="acknowledged", severity=acked["severity"], actor=actor,
                detail={"via": "anomaly_finding", "finding_id": finding_id})
            await audit.record(session, actor=actor, action="alarm.acknowledge",
                               target_type="alarm", target_id=acked["id"], ip=ip,
                               user_agent=agent,
                               before={"state": "ACTIVE", "severity": acked["severity"]},
                               after={"state": "ACKNOWLEDGED", "note": body.note,
                                      "via": "finding"})
            alarm = await alarm_repo.get_alarm(session, acked["id"])
            if alarm:
                actions.append(AlarmAction("alarm_updated", {**alarm, "via": "finding"}))
    await audit.record(session, actor=actor, action="anomaly.finding.feedback",
                       target_type="anomaly_finding", target_id=finding_id, ip=ip,
                       user_agent=agent,
                       before={"state": done["from_state"]},
                       after={"state": done["to_state"], "action": body.action,
                              "reason": body.reason, "note": body.note})
    frames = await anomaly_sweep.build_finding_frames(session, {
        finding_id: ("anomaly.finding.updated", _CHANGE[body.action], "finding")})
    row = await repo.findings_by_ids(session, [finding_id])
    await session.commit()
    await _after_commit(redis, frames=frames, actions=actions)
    return row[0]


# =============================================================================
# detectors
# =============================================================================

def _detector_item(r: dict[str, Any], stats: dict[str, Any],
                   points: dict[str, tuple[str, list]]) -> dict[str, Any]:
    is_l5 = r["id"] == cat.L5_ID
    in_scope = inactive = 0
    if not is_l5:
        for dtype, have in points.values():
            if dtype not in r["device_types"]:
                continue
            if cat.missing_points(r["required_metrics"], have):
                inactive += 1
            else:
                in_scope += 1
    s = stats.get(r["id"], {})
    confirmed, dismissed = int(s.get("confirmed", 0)), int(s.get("dismissed", 0))
    return {
        "id": r["id"], "name": r["name"], "diagnosis": r["diagnosis"],
        "version": r["version"], "layer": f"L{r['layer']}", "phase": r["phase"],
        "alarm_type": r["alarm_type"], "device_types": list(r["device_types"]),
        "primary_metric": r["primary_metric"],
        "required_metrics": list(r["required_metrics"]), "enabled": r["enabled"],
        "lane": None if is_l5 else r["default_lane"],
        "kind": None if is_l5 else r["default_kind"],
        "severity": r["severity"], "cadence_s": r["cadence_s"], "params": r["params"],
        "param_schema": cat.param_schema(r["id"]),
        "devices_in_scope": None if is_l5 else in_scope,
        "inactive_devices": None if is_l5 else inactive,
        "open_findings": int(s.get("open_findings", 0)),
        "confirmed": confirmed, "dismissed": dismissed,
        "precision": (confirmed / (confirmed + dismissed)) if confirmed + dismissed else None,
        "baseline": None,
        "updated_at": r["updated_at"], "updated_by": r["updated_by"],
    }


async def _detector_items(session: AsyncSession, only: str | None = None) -> list[dict]:
    rows = await repo.detector_rows(session)
    if only:
        rows = [r for r in rows if r["id"] == only]
    types = sorted({t for r in rows for t in r["device_types"]})
    points = await repo.device_points(session, types)
    stats = await repo.detector_stats(session)
    return [_detector_item(r, stats, points) for r in rows]


@router.get("/detectors", summary="The detector catalogue, with parameter bounds")
async def list_detectors(session: AsyncSession = Depends(get_session),
                         _: Principal = Depends(require_role("viewer"))) -> dict[str, Any]:
    return {"items": await _detector_items(session)}


@router.patch("/detectors/{detector_id}", summary="Enable, disable or retune a detector")
async def patch_detector(
    detector_id: str, body: DetectorPatch, request: Request,
    session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    row = await repo.detector_row(session, detector_id)
    if row is None:
        raise _fail(404, "unknown_detector", f"no detector {detector_id}")
    try:
        params = cat.validate_params(detector_id, body.params or {})
    except cat.ParamError as exc:
        raise _fail(422, exc.error, str(exc), key=exc.key) from None
    changed = [f"params.{k}" for k, v in params.items() if row["params"].get(k) != v]
    if body.enabled is not None and body.enabled != row["enabled"]:
        changed.insert(0, "enabled")
    if not changed:
        return {"ok": True, "detector_id": detector_id, "version": row["version"],
                "changed": [], "detector": (await _detector_items(session, detector_id))[0]}
    # A parameter change is a new version; enabling is not. The L5 row's
    # params are switches, not detection maths, so they never bump it.
    bump = any(c.startswith("params.") for c in changed) and detector_id != cat.L5_ID
    actor = audit.actor_of(principal)
    new_params = {k: v for k, v in params.items() if f"params.{k}" in changed}
    updated = await repo.update_detector(
        session, detector_id, enabled=body.enabled if "enabled" in changed else None,
        params=new_params or None, bump=bump, actor=actor)
    actions: list = []
    changes: dict = {}
    if "enabled" in changed and body.enabled is False:
        actions, changes = await anomaly_sweep.resolve_findings(
            session, await repo.open_findings_for(session, detector_id),
            alarm_type=row["alarm_type"], resolution="detector_disabled",
            at=datetime.now(UTC), by=actor)
        await integration_outbox.enqueue_actions(session, actions)
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=actor, action="anomaly.detector.edit",
                       target_type="anomaly_detector", target_id=detector_id, ip=ip,
                       user_agent=agent,
                       before={"enabled": row["enabled"], "version": row["version"],
                               "params": {k: row["params"].get(k) for k in new_params}},
                       after={"enabled": body.enabled if body.enabled is not None
                              else row["enabled"], "version": updated["version"],
                              "params": new_params})
    frames = await anomaly_sweep.build_finding_frames(session, changes)
    item = (await _detector_items(session, detector_id))[0]
    await session.commit()
    await _bump_catalogue(redis)
    await _after_commit(redis, frames=frames, actions=actions)
    return {"ok": True, "detector_id": detector_id, "version": updated["version"],
            "changed": changed, "detector": item}


# =============================================================================
# overrides
# =============================================================================

@router.get("/overrides", summary="Per-device detector exceptions")
async def list_overrides(detector: str | None = None, device_id: str | None = None,
                         limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                         with_total: bool = False,
                         session: AsyncSession = Depends(get_session),
                         _: Principal = Depends(require_role("viewer"))) -> dict[str, Any]:
    _check_uuid(device_id, "device_id")
    return await repo.list_overrides(session, detector=detector, device_id=device_id,
                                     limit=limit, offset=offset, with_total=with_total)


@router.put("/overrides/{detector_id}/{device_id}",
            summary="Set one device's exception: params, or a mute with a reason")
async def put_override(
    detector_id: str, device_id: str, body: OverridePut, request: Request,
    session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    row = await repo.detector_row(session, detector_id)
    if row is None or detector_id == cat.L5_ID:
        raise _fail(404, "unknown_detector", f"no per-device detector {detector_id}")
    if not await repo.device_exists(session, device_id):
        raise _fail(404, "not_found", "no such device")
    if body.params is None and body.enabled is None:
        raise _fail(422, "override_noop", "an override must set params or enabled")
    if body.enabled is False and not (body.reason or "").strip():
        raise _fail(422, "mute_needs_reason", "a mute must say why")
    try:
        params = cat.validate_params(detector_id, body.params) if body.params else None
    except cat.ParamError as exc:
        raise _fail(422, exc.error, str(exc), key=exc.key) from None
    if body.params is not None and not params and body.enabled is None:
        raise _fail(422, "override_noop", "an override must set params or enabled")
    before = await repo.get_override(session, detector_id, device_id)
    actor = audit.actor_of(principal)
    await repo.upsert_override(session, detector_id, device_id, params=params,
                               enabled=body.enabled, reason=body.reason,
                               expires_at=body.expires_at, actor=actor)
    actions: list = []
    changes: dict = {}
    if body.enabled is False:
        actions, changes = await anomaly_sweep.resolve_findings(
            session, await repo.open_findings_for(session, detector_id, device_id),
            alarm_type=row["alarm_type"], resolution="muted", at=datetime.now(UTC),
            by=actor)
        await integration_outbox.enqueue_actions(session, actions)
    after = await repo.get_override(session, detector_id, device_id)
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=actor, action="anomaly.override.put",
                       target_type="anomaly_override", target_id=f"{detector_id}/{device_id}",
                       ip=ip, user_agent=agent, before=jsonable_encoder(before),
                       after=jsonable_encoder(after))
    frames = await anomaly_sweep.build_finding_frames(session, changes)
    await session.commit()
    await _bump_catalogue(redis)
    await _after_commit(redis, frames=frames, actions=actions)
    return after


@router.delete("/overrides/{detector_id}/{device_id}", summary="Remove an exception")
async def delete_override(
    detector_id: str, device_id: str, request: Request,
    session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    if await repo.detector_row(session, detector_id) is None:
        raise _fail(404, "unknown_detector", f"no detector {detector_id}")
    if not await repo.device_exists(session, device_id):
        raise _fail(404, "not_found", "no such device")
    before = await repo.get_override(session, detector_id, device_id)
    if before is None or not await repo.delete_override(session, detector_id, device_id):
        raise _fail(404, "not_found", "no such override")
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="anomaly.override.remove", target_type="anomaly_override",
                       target_id=f"{detector_id}/{device_id}", ip=ip, user_agent=agent,
                       before=jsonable_encoder(before), after=None)
    await session.commit()
    await _bump_catalogue(redis)
    return {"ok": True, "detector_id": detector_id, "device_id": device_id}


# =============================================================================
# baseline exclusions
# =============================================================================

@router.get("/exclusions", summary="Spans the nightly fit must not learn as normal")
async def list_exclusions(
    device_id: str | None = None, room: str | None = None, detector: str | None = None,
    source: list[str] | None = Query(None), active: bool | None = None,
    from_: datetime | None = Query(None, alias="from"), to: datetime | None = None,
    limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
    with_total: bool = False, session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("viewer")),
) -> dict[str, Any]:
    _check_enum(source, EXCLUSION_SOURCES, "source")
    _check_uuid(device_id, "device_id")
    _check_uuid(room, "room")
    return await repo.list_exclusions(session, device_id=device_id, room=room,
                                      detector=detector, source=source, active=active,
                                      frm=from_, to=to, limit=limit, offset=offset,
                                      with_total=with_total)


@router.post("/exclusions", status_code=201, summary="Add an exclusion (manual or a "
                                                     "simulator ledger import)")
async def create_exclusion(
    body: ExclusionCreate, request: Request,
    session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis),
    principal: Principal = Depends(require_role("operator")),
):
    scope = [body.device_id, body.room_id, body.datacenter_id]
    if sum(1 for x in scope if x) != 1:
        raise _fail(422, "exclusion_scope",
                    "exactly one of device_id, room_id or datacenter_id")
    for v, what in zip(scope, ("device_id", "room_id", "datacenter_id"), strict=True):
        if v:
            try:
                uuid.UUID(v)
            except ValueError:
                raise _fail(422, "exclusion_scope", f"{what} must be a uuid") from None
    if body.ends_at is not None and body.ends_at <= body.starts_at:
        raise _fail(422, "exclusion_dates_invalid", "ends_at must be after starts_at")
    if body.source not in POSTABLE_SOURCES:
        raise _fail(422, "source_not_allowed",
                    f"source {body.source!r} is system-owned or unknown; post "
                    + " or ".join(POSTABLE_SOURCES))
    if body.detector_id and await repo.detector_row(session, body.detector_id) is None:
        raise _fail(404, "unknown_detector", f"no detector {body.detector_id}")
    if body.metric_key and body.metric_key not in METRICS:
        raise _fail(422, "unknown_metric", f"{body.metric_key} is not a registry key")
    if body.device_id and not await repo.device_exists(session, body.device_id):
        raise _fail(404, "not_found", "no such device")
    actor = audit.actor_of(principal)
    try:
        row, created = await repo.create_exclusion(session, body.model_dump(), actor)
    except Exception as exc:  # an unknown room/datacenter id is an FK violation
        await session.rollback()
        if "foreign key" in str(exc).lower():
            raise _fail(404, "not_found", "no such room or datacenter") from None
        raise
    if created:
        ip, agent = audit.client_of(request)
        await audit.record(session, actor=actor, action="anomaly.exclusion.create",
                           target_type="anomaly_baseline_exclusion", target_id=row["id"],
                           ip=ip, user_agent=agent, before=None, after=jsonable_encoder(row))
    await session.commit()
    if created:
        await _bump_catalogue(redis)
    return JSONResponse(jsonable_encoder(row), status_code=201 if created else 200)


@router.delete("/exclusions/{exclusion_id}", summary="Remove a manual or injected-fault "
                                                     "exclusion")
async def delete_exclusion(
    exclusion_id: str, request: Request,
    session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    row = await repo.get_exclusion(session, exclusion_id)
    if row is None:
        raise _fail(404, "not_found", "no such exclusion")
    if row["source"] in SYSTEM_SOURCES:
        raise _fail(409, "exclusion_system_owned",
                    f"a {row['source']} exclusion follows its window or finding")
    await repo.delete_exclusion(session, exclusion_id)
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="anomaly.exclusion.delete",
                       target_type="anomaly_baseline_exclusion", target_id=exclusion_id,
                       ip=ip, user_agent=agent, before=jsonable_encoder(row), after=None)
    await session.commit()
    await _bump_catalogue(redis)
    return {"ok": True, "id": exclusion_id}


# =============================================================================
# health
# =============================================================================

def _p95(values: list[int]) -> int | None:
    if not values:
        return None
    s = sorted(values)
    return int(s[min(len(s) - 1, math.ceil(0.95 * len(s)) - 1)])


def _parse(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def sweep_detail(outcomes: list[dict], heartbeat: dict | None) -> SweepDetail:
    ran = [e for e in outcomes if e.get("outcome") == "ran"]
    errors = [e for e in outcomes if e.get("outcome") == "error"]
    last_error = (heartbeat or {}).get("error")
    if not last_error and errors:
        last_error = errors[0].get("error")
    return SweepDetail(
        p95_ms_1h=_p95([int(e["ms"]) for e in ran if "ms" in e]),
        runs_1h=len(ran),
        gate_skips_1h=sum(1 for e in outcomes if e.get("outcome") == "cadence"),
        errors_1h=len(errors), last_error=last_error,
        series_scored=int((heartbeat or {}).get("series_scored") or 0))


@router.get("/health", response_model=AnomalyHealth,
            summary="Is the anomaly sweep running, and how is it doing")
async def health(session: AsyncSession = Depends(get_session),
                 redis: Redis = Depends(get_redis),
                 settings: Settings = Depends(get_settings),
                 _: Principal = Depends(require_role("viewer"))) -> AnomalyHealth:
    # severity_cap is the constant the sweep's derived_severity() enforces
    # (docs/33 task 1.18), reported even when the feature is off.
    base = {"enabled": settings.anomaly_enabled, "explainer": settings.anomaly_explainer,
            "sweep_s": settings.anomaly_sweep_s, "severity_cap": DERIVED_SEVERITY_CAP}
    if not settings.anomaly_enabled:
        return AnomalyHealth(**base)

    now = datetime.now(UTC)
    # Redis first and tolerant: a Redis outage must not 500 the health view,
    # it is exactly when somebody opens it.
    hb = baseline_hb = None
    outcomes: list[dict] = []
    try:
        hb = await anomaly_sweep.read_heartbeat(redis)
        baseline_hb = await anomaly_sweep.read_heartbeat(
            redis, anomaly_sweep.BASELINE_HEARTBEAT_KEY)
        outcomes = await anomaly_sweep.read_outcomes(redis, now)
    except Exception as exc:
        log.warning("anomaly health could not read redis", error=str(exc))

    counts = await repo.health_counts(session)

    baselines = BaselineDetail()
    if baseline_hb:
        start, end = _parse(baseline_hb.get("started_at")), _parse(baseline_hb.get("finished_at"))
        baselines = BaselineDetail(
            last_fit_duration_s=(end - start).total_seconds() if start and end else None,
            series_fitted=int(baseline_hb.get("series_fitted") or 0),
            rejected=int(baseline_hb.get("rejected") or 0))

    return AnomalyHealth(
        **base,
        last_sweep_at=_parse((hb or {}).get("finished_at")),
        last_sweep_ms=(hb or {}).get("duration_ms"),
        findings_per_hour=counts["findings_per_hour"],
        budget_per_hour=None,
        lock_holder=(hb or {}).get("consumer"),
        baseline_last_fit_at=counts["baseline_last_fit_at"],
        l5_major_enabled=counts["l5_major_enabled"],
        detail=HealthDetail(
            sweep=sweep_detail(outcomes, hb),
            baselines=baselines,
            findings=FindingsDetail(**counts["findings"]),
            incidents=IncidentsDetail(**counts["incidents"])))

