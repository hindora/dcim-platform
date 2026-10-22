"""Reads and writes for the anomaly tables (migration 0111).

Raw SQL, like the alarm repository: the seven anomaly_* tables have no ORM
class (docs/29 section 1).

Phase 0 holds three things here:

* the catalogue load (anomaly_detector + live overrides);
* the accumulator checkpoint and restore rules (docs/29 sections 10.3-10.4);
* the figures behind GET /anomalies/health.

The finding/incident writes arrive with Phase 1.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.catalogue import Catalogue, Detector, Override

#: Every CHECKPOINT_EVERY_SWEEPS sweeps the dirty L3/L4 series are upserted
#: (300 s at the default cadence), bounding the loss when Redis and the
#: worker die together (docs/29 section 10.3).
CHECKPOINT_EVERY_SWEEPS = 5
#: A checkpoint older than this is reset rather than resumed: a CUSUM must
#: not integrate across a six-hour hole (docs/29 section 10.4).
MAX_RESTORE_AGE_S = 21_600
#: The state document's schema version (docs/29 section 7).
STATE_VERSION = 1


# --- catalogue -----------------------------------------------------------------

async def load_catalogue(session: AsyncSession, now: datetime | None = None) -> Catalogue:
    """Every detector row, and the overrides that have not expired.

    About sixty rows; read whole, no index (docs/29 section 4.1).
    """
    det = (await session.execute(text("""
        SELECT id, version, layer, phase, name, diagnosis, device_types,
               primary_metric, required_metrics, default_lane, default_kind,
               alarm_type, severity::text AS severity, cadence_s, params, enabled
          FROM anomaly_detector
         ORDER BY id
    """))).mappings().all()
    ov = (await session.execute(text("""
        SELECT detector_id, device_id::text AS device_id, params, enabled,
               expires_at, reason
          FROM anomaly_override
         WHERE expires_at IS NULL OR expires_at > COALESCE(CAST(:now AS timestamptz), now())
    """), {"now": now})).mappings().all()
    return Catalogue([Detector.from_row(r) for r in det],
                     [Override.from_row(r) for r in ov])


# --- checkpoint / restore --------------------------------------------------------

@dataclass(frozen=True)
class SeriesKey:
    detector_id: str
    device_id: str
    metric_id: int
    instance: str = ""

    def acc_field(self) -> str:
        """The field in the dcim:anomaly:acc hash (docs/29 section 10)."""
        return f"{self.detector_id}|{self.device_id}|{self.metric_id}|{self.instance}"

    @classmethod
    def from_acc_field(cls, field: str) -> SeriesKey:
        detector_id, device_id, metric_id, instance = field.split("|", 3)
        return cls(detector_id, device_id, int(metric_id), instance)


@dataclass(frozen=True)
class Checkpoint:
    key: SeriesKey
    detector_version: int
    state: Mapping[str, Any]
    baseline_id: str | None = None
    last_sample_at: datetime | None = None
    updated_at: datetime | None = None


async def checkpoint(session: AsyncSession, rows: Iterable[Checkpoint],
                     now: datetime | None = None) -> int:
    """Upsert dirty series in one statement (docs/29 section 10.3).

    `WHERE EXCLUDED.updated_at > anomaly_state.updated_at` means an older
    copy - a slow worker, a replayed flush - never overwrites a newer one.
    Returns the rows written.
    """
    rows = list(rows)
    if not rows:
        return 0
    at = now or datetime.now(UTC)
    params = {
        "det": [r.key.detector_id for r in rows],
        "dev": [r.key.device_id for r in rows],
        "met": [r.key.metric_id for r in rows],
        "inst": [r.key.instance for r in rows],
        "ver": [r.detector_version for r in rows],
        "base": [r.baseline_id for r in rows],
        "state": [json.dumps(dict(r.state)) for r in rows],
        "last": [r.last_sample_at for r in rows],
        "upd": [r.updated_at or at for r in rows],
    }
    result = await session.execute(text("""
        INSERT INTO anomaly_state (detector_id, device_id, metric_id, instance,
                                   detector_version, baseline_id, state,
                                   last_sample_at, updated_at)
        SELECT u.det, u.dev, u.met, u.inst, u.ver, u.base, u.state::jsonb, u.last, u.upd
          FROM unnest(CAST(:det AS text[]), CAST(:dev AS uuid[]),
                      CAST(:met AS smallint[]), CAST(:inst AS text[]),
                      CAST(:ver AS integer[]), CAST(:base AS uuid[]),
                      CAST(:state AS text[]), CAST(:last AS timestamptz[]),
                      CAST(:upd AS timestamptz[]))
               AS u(det, dev, met, inst, ver, base, state, last, upd)
        ON CONFLICT (detector_id, device_id, metric_id, instance) DO UPDATE SET
            detector_version = EXCLUDED.detector_version,
            baseline_id      = EXCLUDED.baseline_id,
            state            = EXCLUDED.state,
            last_sample_at   = EXCLUDED.last_sample_at,
            updated_at       = EXCLUDED.updated_at
         WHERE EXCLUDED.updated_at > anomaly_state.updated_at
    """), params)
    return result.rowcount or 0


async def load_checkpoints(session: AsyncSession,
                           keys: Iterable[SeriesKey]) -> dict[SeriesKey, Checkpoint]:
    """The stored rows for these series, for a restore after a Redis miss."""
    keys = list(keys)
    if not keys:
        return {}
    rows = (await session.execute(text("""
        SELECT s.detector_id, s.device_id::text AS device_id, s.metric_id, s.instance,
               s.detector_version, s.baseline_id::text AS baseline_id, s.state,
               s.last_sample_at, s.updated_at
          FROM anomaly_state s
          JOIN unnest(CAST(:det AS text[]), CAST(:dev AS uuid[]),
                      CAST(:met AS smallint[]), CAST(:inst AS text[]))
               AS k(det, dev, met, inst)
            ON s.detector_id = k.det AND s.device_id = k.dev
           AND s.metric_id = k.met AND s.instance = k.inst
    """), {"det": [k.detector_id for k in keys], "dev": [k.device_id for k in keys],
           "met": [k.metric_id for k in keys], "inst": [k.instance for k in keys]}
    )).mappings().all()
    out = {}
    for r in rows:
        key = SeriesKey(r["detector_id"], r["device_id"], int(r["metric_id"]), r["instance"])
        out[key] = Checkpoint(key=key, detector_version=int(r["detector_version"]),
                              state=dict(r["state"] or {}), baseline_id=r["baseline_id"],
                              last_sample_at=r["last_sample_at"], updated_at=r["updated_at"])
    return out


@dataclass(frozen=True)
class Restored:
    state: dict[str, Any]
    #: 'redis' | 'checkpoint' | 'reset' | 'fresh'
    source: str
    reset_reason: str | None = None


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_ts(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def fresh_state(*, detector_version: int, baseline_id: str | None,
                reset: Mapping[str, Any] | None = None) -> dict[str, Any]:
    st: dict[str, Any] = {"v": STATE_VERSION, "detector_version": detector_version,
                          "baseline_id": baseline_id}
    if reset is not None:
        st["reset"] = dict(reset)
    return st


def restore(*, redis_state: Mapping[str, Any] | None, stored: Checkpoint | None,
            detector_version: int, baseline_id: str | None, now: datetime,
            max_age_s: int = MAX_RESTORE_AGE_S) -> Restored:
    """Choose the accumulator to resume from (docs/29 section 10.4).

    * Redis holds the hot copy. When it is present and at least as new as
      the checkpoint (by `last_ts`), Redis wins.
    * Otherwise the checkpoint is used - but RESET rather than resumed, with
      `state.reset` recorded, when it is older than `max_age_s`, was written
      by another detector version, or scored against another baseline.
      Residuals against a different model are not additive, and a CUSUM
      that integrates across a hole is evidence about nothing.
    * With neither, the series starts fresh.

    The same version/baseline test applies to a Redis copy: a PATCH that
    bumps the version resets accumulators (docs/30 section 6.2).
    """
    def stale_reason(st: Mapping[str, Any], *, updated_at: datetime | None) -> str | None:
        if updated_at is not None and (now - updated_at).total_seconds() > max_age_s:
            return "restore_too_old"
        if int(st.get("detector_version", detector_version)) != detector_version:
            return "detector_version"
        if st.get("baseline_id") != baseline_id:
            return "baseline_changed"
        return None

    def reset(reason: str) -> Restored:
        return Restored(fresh_state(detector_version=detector_version,
                                    baseline_id=baseline_id,
                                    reset={"reason": reason, "at": _iso(now)}),
                        "reset", reason)

    stored_ts = None
    if stored is not None:
        stored_ts = _parse_ts(stored.state.get("last_ts")) or stored.last_sample_at
    if redis_state is not None:
        redis_ts = _parse_ts(redis_state.get("last_ts"))
        if stored is None or stored_ts is None or (redis_ts is not None and redis_ts >= stored_ts):
            why = stale_reason(redis_state, updated_at=None)
            return reset(why) if why else Restored(dict(redis_state), "redis")

    if stored is not None:
        st = dict(stored.state)
        st.setdefault("detector_version", stored.detector_version)
        st.setdefault("baseline_id", stored.baseline_id)
        why = stale_reason(st, updated_at=stored.updated_at)
        if why:
            return reset(why)
        st.pop("reset", None)
        return Restored(st, "checkpoint")

    return Restored(fresh_state(detector_version=detector_version,
                                baseline_id=baseline_id), "fresh")


# --- health ------------------------------------------------------------------------

async def health_counts(session: AsyncSession) -> dict[str, Any]:
    """The database half of GET /anomalies/health (docs/30 section 11).

    Every figure is an index-backed count over small tables:
    ix_anomaly_finding_active for the open set, ix_anomaly_finding_first_seen
    for the hour and the day, ux_anomaly_incident_open_root for incidents.
    """
    f = (await session.execute(text("""
        SELECT count(*) FILTER (WHERE state <> 'resolved')                    AS open,
               count(*) FILTER (WHERE first_seen > now() - interval '24 hours') AS opened_24h,
               count(*) FILTER (WHERE first_seen > now() - interval '1 hour')   AS opened_1h,
               count(*) FILTER (WHERE state <> 'resolved' AND lane = 'advisory') AS advisory,
               count(*) FILTER (WHERE state <> 'resolved' AND lane = 'alert')    AS alert,
               count(*) FILTER (WHERE state <> 'resolved' AND kind = 'sensor')   AS sensor,
               count(*) FILTER (WHERE state <> 'resolved' AND kind = 'process')  AS process,
               count(*) FILTER (WHERE state <> 'resolved' AND kind = 'undetermined')
                                                                                 AS undetermined
          FROM anomaly_finding
         WHERE state <> 'resolved' OR first_seen > now() - interval '24 hours'
    """))).mappings().one()
    incidents_open = await session.scalar(text(
        "SELECT count(*) FROM anomaly_incident WHERE state = 'open'"))
    last_fit = await session.scalar(text("SELECT max(fitted_at) FROM anomaly_baseline"))
    l5 = (await session.execute(text("""
        SELECT enabled, COALESCE((params ->> 'major_enabled')::boolean, false) AS major
          FROM anomaly_detector WHERE id = 'L5'
    """))).mappings().first()
    return {
        "findings": {
            "open": int(f["open"]), "opened_24h": int(f["opened_24h"]),
            "by_lane": {"advisory": int(f["advisory"]), "alert": int(f["alert"])},
            "by_kind": {"sensor": int(f["sensor"]), "process": int(f["process"]),
                        "undetermined": int(f["undetermined"])},
        },
        "findings_per_hour": int(f["opened_1h"]),
        "incidents": {"open": int(incidents_open or 0)},
        "baseline_last_fit_at": last_fit,
        "l5_major_enabled": bool(l5 and l5["enabled"] and l5["major"]),
    }


async def baseline_fitted_since(session: AsyncSession, since: datetime) -> bool:
    """The once-per-UTC-day guard (docs/29 section 10.1). In Postgres, not
    Redis, so a Redis flush cannot trigger a second full fit."""
    last = await session.scalar(text("SELECT max(fitted_at) FROM anomaly_baseline"))
    return last is not None and last >= since


def fit_day_start(now: datetime, hour_utc: int) -> datetime:
    """The most recent `hour_utc`:00 UTC at or before `now`."""
    n = now.astimezone(UTC)
    start = n.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    return start if start <= n else start - timedelta(days=1)


# =============================================================================
# Phase 1: the sweep's reads and writes
# =============================================================================

#: Lifecycle states whose open findings resolve as `device_retired`.
RETIRED_LIFECYCLES = ("decommissioned", "retired")

#: Alarm types whose open presence in a room is "gas pre-discharge": the
#: release panel has counted down or released, and the simulator (and a real
#: interlock) stops every CRAH in the room (docs/29 section 7.1 gate note).
FIRE_PREDISCHARGE_TYPES = ("gas_release_imminent", "gas_agent_released")


async def metric_ids(session: AsyncSession) -> dict[str, int]:
    rows = (await session.execute(text("SELECT key, id FROM metric"))).all()
    return {k: int(i) for k, i in rows}


async def sweep_devices(session: AsyncSession, device_types: list[str]) -> list[dict[str, Any]]:
    """Every device of a scored type, with what the gates need.

    `online`: any enabled endpoint ONLINE with a success in the last 300 s
    (the staleness test's reachable half). `poll_interval_s`: the slowest
    enabled endpoint's interval, which DQ-1's grace is measured against.
    """
    if not device_types:
        return []
    rows = (await session.execute(text("""
        SELECT d.id::text AS device_id, d.name, d.device_type, d.lifecycle::text AS lifecycle,
               rm.id::text AS room_id, rm.datacenter_id::text AS datacenter_id,
               dt.category AS role,
               COALESCE(ep.online, false) AS online, ep.poll_interval_s,
               EXISTS (SELECT 1 FROM maintenance_target t
                         JOIN maintenance_window w ON w.id = t.window_id
                        WHERE t.device_id = d.id AND w.status = 'active' AND w.suppress)
                   AS maintenance,
               (rm.id IS NOT NULL AND EXISTS (
                    SELECT 1 FROM alarm a
                      JOIN device fd ON fd.id = a.device_id
                      LEFT JOIN rack fr ON fr.id = fd.rack_id
                      LEFT JOIN rack_row frr ON frr.id = fr.row_id
                     WHERE a.alarm_type = ANY(:fire) AND a.state <> 'CLEARED'
                       AND COALESCE(frr.room_id, fd.room_id) = rm.id)) AS fire_predischarge
          FROM device d
          JOIN device_type dt   ON dt.code = d.device_type
          LEFT JOIN rack r      ON r.id = d.rack_id
          LEFT JOIN rack_row rr ON rr.id = r.row_id
          LEFT JOIN room rm     ON rm.id = COALESCE(rr.room_id, d.room_id)
          LEFT JOIN LATERAL (
              SELECT bool_or(st.status = 'ONLINE'
                             AND st.last_success > now() - interval '300 seconds') AS online,
                     max(p.interval_s) AS poll_interval_s
                FROM device_endpoint e
                LEFT JOIN endpoint_state st ON st.endpoint_id = e.id
                LEFT JOIN poll_profile p    ON p.id = e.poll_profile_id
               WHERE e.device_id = d.id AND e.enabled) ep ON true
         WHERE d.device_type = ANY(:types)
    """), {"types": list(device_types), "fire": list(FIRE_PREDISCHARGE_TYPES)})).mappings().all()
    return [dict(r) for r in rows]


async def read_numeric(session: AsyncSession, since_by_device: Mapping[str, datetime],
                       metric_id_list: list[int], floor: datetime, now: datetime
                       ) -> list[tuple[str, int, str, float, float, str]]:
    """New numeric samples per device, through the (device, metric, instance,
    ts) primary key. `ts > floor` lets Timescale exclude old chunks."""
    if not since_by_device or not metric_id_list:
        return []
    devs = list(since_by_device)
    rows = (await session.execute(text("""
        SELECT t.device_id::text, t.metric_id, t.instance,
               EXTRACT(epoch FROM t.ts)::float8, t.value::float8, t.quality
          FROM telemetry_sample t
          JOIN unnest(CAST(:dev AS uuid[]), CAST(:since AS timestamptz[])) AS k(dev, since)
            ON t.device_id = k.dev AND t.ts > k.since
         WHERE t.metric_id = ANY(:mids) AND t.ts > :floor AND t.ts <= :now
    """), {"dev": devs, "since": [since_by_device[d] for d in devs],
           "mids": metric_id_list, "floor": floor, "now": now})).all()
    return [tuple(r) for r in rows]


async def read_bools(session: AsyncSession, since_by_device: Mapping[str, datetime],
                     metric_id_list: list[int], floor: datetime, now: datetime
                     ) -> list[tuple[str, int, str, float, bool, str]]:
    """Change-logged boolean rows. The floor is the last-known look-back
    (1800 s), so a heartbeat row is always inside a first read."""
    if not since_by_device or not metric_id_list:
        return []
    devs = list(since_by_device)
    rows = (await session.execute(text("""
        SELECT t.device_id::text, t.metric_id, t.instance,
               EXTRACT(epoch FROM t.ts)::float8, t.value, t.quality
          FROM telemetry_bool t
          JOIN unnest(CAST(:dev AS uuid[]), CAST(:since AS timestamptz[])) AS k(dev, since)
            ON t.device_id = k.dev AND t.ts > k.since
         WHERE t.metric_id = ANY(:mids) AND t.ts > :floor AND t.ts <= :now
    """), {"dev": devs, "since": [since_by_device[d] for d in devs],
           "mids": metric_id_list, "floor": floor, "now": now})).all()
    return [tuple(r) for r in rows]


async def open_findings(session: AsyncSession) -> list[dict[str, Any]]:
    """Every non-resolved L0/L2 finding: the sweep's view of the DB truth.

    Small (~100 rows at the design budget, docs/29 section 6.2), read through
    ix_anomaly_finding_active.
    """
    rows = (await session.execute(text("""
        SELECT f.id::text, f.detector_id, f.device_id::text AS device_id, f.metric_id,
               f.instance, f.state, f.lane, f.kind, f.alarm_id::text AS alarm_id,
               f.reopen_count, f.first_seen, f.verdict, f.occurrences
          FROM anomaly_finding f
         WHERE f.state <> 'resolved' AND f.layer IN (0, 2)
    """))).mappings().all()
    return [dict(r) for r in rows]


async def open_finding(session: AsyncSession, *, detector_id: str, detector_version: int,
                       layer: int, lane: str, kind: str, device_id: str, metric_id: int,
                       instance: str, datacenter_id: str | None, room_id: str | None,
                       first_seen: datetime, now: datetime, reopen_window_s: int,
                       score: float | None, expected: float | None,
                       observed: float | None, summary: str,
                       evidence: dict[str, Any]) -> dict[str, Any]:
    """Open a finding: reopen the same row inside the window, else insert.

    Under the sweep lock, so the reopen lookup cannot race an insert
    (docs/29 section 8.1). An already-open row (a Redis loss forgot it was
    active) is touched through the dedupe key instead. Returns the row's
    id plus `outcome` = 'opened' | 'reopened' | 'existing', and `alarm_id`.
    """
    params = {"det": detector_id, "ver": detector_version, "layer": layer, "lane": lane,
              "kind": kind, "dev": device_id, "met": metric_id, "inst": instance,
              "dc": datacenter_id, "room": room_id, "first": first_seen, "now": now,
              "win": reopen_window_s, "score": score, "exp": expected, "obs": observed,
              "summary": summary, "ev": json.dumps(evidence)}
    row = (await session.execute(text("""
        UPDATE anomaly_finding f
           SET state = 'open', resolved_at = NULL, resolved_by = NULL, resolution = NULL,
               reopen_count = f.reopen_count + 1, raised_at = :now, last_seen = :now,
               occurrences = f.occurrences + 1, score = :score,
               score_peak = GREATEST(f.score_peak, :score), expected = :exp,
               observed = :obs, summary = :summary,
               evidence = CAST(:ev AS jsonb)
                          || jsonb_build_object('alarm_history',
                                 COALESCE(f.evidence -> 'alarm_history', '[]'::jsonb)),
               detector_version = :ver, updated_at = now()
         WHERE f.id = (SELECT r.id FROM anomaly_finding r
                        WHERE r.detector_id = :det AND r.device_id = CAST(:dev AS uuid)
                          AND r.metric_id = :met AND r.instance = :inst
                          AND r.state = 'resolved'
                          AND r.resolved_at > CAST(:now AS timestamptz)
                                              - make_interval(secs => :win)
                        ORDER BY r.resolved_at DESC LIMIT 1)
           AND NOT EXISTS (SELECT 1 FROM anomaly_finding o
                            WHERE o.detector_id = :det AND o.device_id = CAST(:dev AS uuid)
                              AND o.metric_id = :met AND o.instance = :inst
                              AND o.state <> 'resolved')
        RETURNING f.id::text AS id, f.alarm_id::text AS alarm_id, f.reopen_count,
                  'reopened' AS outcome
    """), params)).mappings().first()
    if row:
        return dict(row)
    row = (await session.execute(text("""
        INSERT INTO anomaly_finding (detector_id, detector_version, layer, lane, kind,
                                     device_id, metric_id, instance, datacenter_id, room_id,
                                     first_seen, raised_at, last_seen, score, score_peak,
                                     expected, observed, summary, evidence)
        VALUES (:det, :ver, :layer, :lane, :kind, CAST(:dev AS uuid), :met, :inst,
                CAST(:dc AS uuid), CAST(:room AS uuid),
                LEAST(CAST(:first AS timestamptz), CAST(:now AS timestamptz)), :now, :now,
                :score, :score, :exp, :obs, :summary, CAST(:ev AS jsonb))
        ON CONFLICT (detector_id, device_id, metric_id, instance) WHERE state <> 'resolved'
        DO UPDATE SET last_seen = EXCLUDED.last_seen,
                      occurrences = anomaly_finding.occurrences + 1,
                      score = EXCLUDED.score, observed = EXCLUDED.observed
        RETURNING id::text AS id, alarm_id::text AS alarm_id, reopen_count,
                  CASE WHEN xmax = 0 THEN 'opened' ELSE 'existing' END AS outcome
    """), params)).mappings().first()
    return dict(row)


async def touch_findings(session: AsyncSession, touches: list[dict[str, Any]],
                         now: datetime) -> int:
    """One statement for every still-true finding: HOT columns only
    (last_seen, occurrences, score, score_peak, observed, updated_at)."""
    if not touches:
        return 0
    res = await session.execute(text("""
        UPDATE anomaly_finding f
           SET last_seen = :now, occurrences = f.occurrences + 1, score = u.score,
               score_peak = GREATEST(f.score_peak, u.score), observed = u.obs,
               updated_at = :now
          FROM unnest(CAST(:ids AS uuid[]), CAST(:score AS float8[]), CAST(:obs AS float8[]))
               AS u(id, score, obs)
         WHERE f.id = u.id AND f.state <> 'resolved'
    """), {"ids": [t["id"] for t in touches], "score": [t.get("score") for t in touches],
           "obs": [t.get("observed") for t in touches], "now": now})
    return res.rowcount or 0


async def resolve_finding(session: AsyncSession, finding_id: str, *, resolution: str,
                          at: datetime, by: str = "system") -> dict[str, Any] | None:
    """Resolve, keeping `verdict` (docs/29 D3). A confirmed process
    finding's baseline exclusion is closed at the same moment (section 8.3)."""
    row = (await session.execute(text("""
        UPDATE anomaly_finding
           SET state = 'resolved', resolved_at = :at, resolved_by = :by,
               resolution = :res, updated_at = now()
         WHERE id = CAST(:id AS uuid) AND state <> 'resolved'
        RETURNING id::text, alarm_id::text AS alarm_id, device_id::text AS device_id,
                  detector_id, instance, verdict
    """), {"id": finding_id, "at": at, "by": by, "res": resolution})).mappings().first()
    if row:
        await session.execute(text("""
            UPDATE anomaly_baseline_exclusion SET ends_at = GREATEST(:at, starts_at
                                                         + interval '1 second')
             WHERE source = 'confirmed_finding' AND source_ref = :id AND ends_at IS NULL
        """), {"id": finding_id, "at": at})
    return dict(row) if row else None


async def link_alarm(session: AsyncSession, finding_id: str, alarm_id: str) -> None:
    """Point the finding at its (new) alarm; an older alarm id goes into
    evidence.alarm_history (docs/29 section 9.2)."""
    await session.execute(text("""
        UPDATE anomaly_finding
           SET evidence = CASE
                   WHEN alarm_id IS NOT NULL AND alarm_id <> CAST(:a AS uuid)
                   THEN jsonb_set(evidence, '{alarm_history}',
                                  COALESCE(evidence -> 'alarm_history', '[]'::jsonb)
                                  || to_jsonb(alarm_id::text))
                   ELSE evidence END,
               alarm_id = CAST(:a AS uuid), promoted_at = COALESCE(promoted_at, now())
         WHERE id = CAST(:id AS uuid)
    """), {"id": finding_id, "a": alarm_id})


async def sync_alarm_acks(session: AsyncSession, now: datetime) -> list[str]:
    """Alarm -> finding acknowledgement, next sweep (docs/30 section 4.4).

    The alarm is authoritative; an `open` alert-lane finding whose alarm is
    ACKNOWLEDGED becomes `acknowledged` with a feedback element via 'alarm'.
    """
    rows = (await session.execute(text("""
        UPDATE anomaly_finding f
           SET state = 'acknowledged', acknowledged_at = COALESCE(a.acknowledged_at, :now),
               acknowledged_by = a.acknowledged_by,
               feedback = f.feedback || jsonb_build_array(jsonb_build_object(
                   'at', to_jsonb(COALESCE(a.acknowledged_at, :now)),
                   'by', a.acknowledged_by, 'action', 'acknowledge', 'reason', NULL,
                   'note', a.ack_note, 'from_state', 'open', 'to_state', 'acknowledged',
                   'detector_version', f.detector_version, 'via', 'alarm')),
               updated_at = now()
          FROM alarm a
         WHERE a.id = f.alarm_id AND a.state = 'ACKNOWLEDGED'
           AND f.state = 'open' AND f.lane = 'alert'
        RETURNING f.id::text
    """), {"now": now})).scalars().all()
    return list(rows)


# =============================================================================
# Phase 1: the API's reads and writes
# =============================================================================

FINDING_STATES = ("open", "acknowledged", "dismissed", "confirmed", "resolved")
DEFAULT_STATES = ("open", "acknowledged", "confirmed")
LANES = ("advisory", "alert")
KINDS = ("sensor", "process", "undetermined")
DISMISS_REASONS = ("not_a_fault", "expected_operation", "sensor_fault", "duplicate")

#: What the finding's `score` is measured in (docs/30 section 4.1). Contract
#: deviation: an L2 margin score is in the rule's own unit (K, %/h), which is
#: what the operator compares against the threshold; `ratio` is used where the
#: score is a count over its limit.
SCORE_UNIT = {"DQ-1": "seconds", "DQ-2": "ratio", "DQ-4": "ratio", "CH-4": "ratio",
              "CR-6": "ratio", "VA-2": "ratio", "AT-1": "ratio", "CH-5": "K/min",
              "RM-2": "K/h", "VA-1": "pct", "PU-3": "pct", "CT-2": "K/h", "CT-3": "%/h"}

_FINDING_SELECT = """
    SELECT f.id::text AS id, f.detector_id, d.name AS detector_name, f.detector_version,
           'L' || f.layer::text AS layer, f.summary, f.device_id::text AS device_id,
           dv.name AS device_name, dv.device_type, m.key AS metric_key, f.instance,
           m.unit, f.kind, f.lane, f.state, f.verdict,
           CASE WHEN f.lane = 'alert' THEN a.severity::text END AS severity,
           f.first_seen, f.raised_at, f.last_seen, f.occurrences, f.reopen_count,
           f.score, f.expected, f.observed, f.impact_kw::float8 AS impact_kw,
           (f.state = 'open' AND f.first_seen < now() - interval '24 hours'
            AND f.feedback = '[]'::jsonb) AS stale,
           f.incident_id::text AS incident_id, f.alarm_id::text AS alarm_id,
           dc.code AS datacenter_code, f.room_id::text AS room_id, rm.name AS room_name,
           rk.name AS rack_name, f.resolved_at, f.resolution
      FROM anomaly_finding f
      JOIN anomaly_detector d ON d.id = f.detector_id
      JOIN device dv          ON dv.id = f.device_id
      JOIN metric m           ON m.id = f.metric_id
      LEFT JOIN alarm a       ON a.id = f.alarm_id
      LEFT JOIN datacenter dc ON dc.id = f.datacenter_id
      LEFT JOIN room rm       ON rm.id = f.room_id
      LEFT JOIN rack rk       ON rk.id = dv.rack_id
"""


def _finding_out(r: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(r)
    out["score_unit"] = SCORE_UNIT.get(out["detector_id"]) or (
        "sigma" if out["layer"] in ("L3", "L4") else (out.get("unit") or "ratio"))
    return out


def _finding_where(*, states, lanes, kinds, detectors, device_id, datacenter, room,
                   since, alarm_id, incident_id, stale) -> tuple[list[str], dict[str, Any]]:
    w: list[str] = []
    p: dict[str, Any] = {}
    if states:
        w.append("f.state = ANY(:states)")
        p["states"] = list(states)
    if lanes:
        w.append("f.lane = ANY(:lanes)")
        p["lanes"] = list(lanes)
    if kinds:
        w.append("f.kind = ANY(:kinds)")
        p["kinds"] = list(kinds)
    if detectors:
        w.append("f.detector_id = ANY(:detectors)")
        p["detectors"] = list(detectors)
    if device_id:
        w.append("f.device_id = CAST(:device_id AS uuid)")
        p["device_id"] = device_id
    if datacenter:
        w.append("(f.datacenter_id::text = :dc OR f.datacenter_id = "
                 "(SELECT id FROM datacenter WHERE code = :dc))")
        p["dc"] = datacenter
    if room:
        w.append("f.room_id = CAST(:room AS uuid)")
        p["room"] = room
    if since:
        w.append("f.last_seen >= :since")
        p["since"] = since
    if alarm_id:
        w.append("f.alarm_id = CAST(:alarm_id AS uuid)")
        p["alarm_id"] = alarm_id
    if incident_id:
        w.append("f.incident_id = CAST(:incident_id AS uuid)")
        p["incident_id"] = incident_id
    if stale is not None:
        cond = ("(f.state = 'open' AND f.first_seen < now() - interval '24 hours' "
                "AND f.feedback = '[]'::jsonb)")
        w.append(cond if stale else f"NOT {cond}")
    return w, p


async def list_findings(session: AsyncSession, *, states=DEFAULT_STATES, lanes=None,
                        kinds=None, detectors=None, device_id=None, datacenter=None,
                        room=None, since=None, alarm_id=None, incident_id=None,
                        stale=None, limit: int = 50, offset: int = 0,
                        with_total: bool = False, with_facets: bool = False
                        ) -> dict[str, Any]:
    filters = {"lanes": lanes, "kinds": kinds, "detectors": detectors,
               "device_id": device_id, "datacenter": datacenter, "room": room,
               "since": since, "alarm_id": alarm_id, "incident_id": incident_id,
               "stale": stale}
    w, p = _finding_where(states=states, **filters)
    where = (" WHERE " + " AND ".join(w)) if w else ""
    rows = (await session.execute(text(
        _FINDING_SELECT + where
        + " ORDER BY f.impact_kw DESC NULLS LAST, f.last_seen DESC, f.id"
          " LIMIT :limit OFFSET :offset"), {**p, "limit": limit, "offset": offset}
    )).mappings().all()
    out: dict[str, Any] = {"items": [_finding_out(r) for r in rows], "next_cursor": None,
                           "total": None}
    if with_total:
        out["total"] = int(await session.scalar(text(
            "SELECT count(*) FROM anomaly_finding f" + where), p) or 0)
    if with_facets:
        out["facets"] = await finding_facets(session, filters)
    return out


async def finding_facets(session: AsyncSession, filters: dict[str, Any]) -> dict[str, Any]:
    """Counts over the same filters minus `state` (docs/30 section 4.1)."""
    w, p = _finding_where(states=None, **filters)
    where = (" WHERE " + " AND ".join(w)) if w else ""
    r = (await session.execute(text(f"""
        SELECT count(*) FILTER (WHERE f.state <> 'resolved')                      AS open_findings,
               count(*) FILTER (WHERE f.state <> 'resolved' AND f.kind = 'sensor') AS open_sensor,
               count(*) FILTER (WHERE f.first_seen > now() - interval '24 hours') AS new_24h,
               count(*) FILTER (WHERE f.state = 'open')                            AS s_open,
               count(*) FILTER (WHERE f.state = 'acknowledged')                    AS s_ack,
               count(*) FILTER (WHERE f.state = 'confirmed')                       AS s_conf,
               count(*) FILTER (WHERE f.state = 'dismissed')                       AS s_dis,
               count(*) FILTER (WHERE f.state = 'resolved')                        AS s_res,
               count(*) FILTER (WHERE f.state <> 'resolved' AND f.lane = 'advisory') AS l_adv,
               count(*) FILTER (WHERE f.state <> 'resolved' AND f.lane = 'alert')    AS l_alert,
               count(*) FILTER (WHERE f.state <> 'resolved' AND f.kind = 'process')  AS k_proc,
               count(*) FILTER (WHERE f.state <> 'resolved' AND f.kind = 'undetermined')
                                                                                     AS k_und
          FROM anomaly_finding f {where}
    """), p)).mappings().one()
    incidents = await session.scalar(text(
        "SELECT count(*) FROM anomaly_incident WHERE state = 'open'"))
    return {
        "open_incidents": int(incidents or 0), "open_findings": int(r["open_findings"]),
        "open_sensor": int(r["open_sensor"]), "new_24h": int(r["new_24h"]),
        "by_state": {"open": int(r["s_open"]), "acknowledged": int(r["s_ack"]),
                     "confirmed": int(r["s_conf"]), "dismissed": int(r["s_dis"]),
                     "resolved": int(r["s_res"])},
        "by_lane": {"advisory": int(r["l_adv"]), "alert": int(r["l_alert"])},
        "by_kind": {"sensor": int(r["open_sensor"]), "process": int(r["k_proc"]),
                    "undetermined": int(r["k_und"])},
    }


async def findings_by_ids(session: AsyncSession, ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    rows = (await session.execute(text(
        _FINDING_SELECT + " WHERE f.id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids}
    )).mappings().all()
    return [_finding_out(r) for r in rows]


async def get_finding(session: AsyncSession, finding_id: str) -> dict[str, Any] | None:
    try:
        UUID(finding_id)
    except ValueError:
        return None
    row = (await session.execute(text(
        _FINDING_SELECT.replace("f.resolved_at, f.resolution",
                                "f.resolved_at, f.resolution, f.evidence, f.feedback")
        + " WHERE f.id = CAST(:id AS uuid)"), {"id": finding_id})).mappings().first()
    return _finding_out(row) if row else None


#: The operator state machine (docs/30 section 4.3): action -> (from, to).
TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "acknowledge": (("open",), "acknowledged"),
    "dismiss": (("open", "acknowledged"), "dismissed"),
    "confirm": (("open", "acknowledged"), "confirmed"),
    "reopen": (("acknowledged", "confirmed", "dismissed"), "open"),
}


def allowed_actions(state: str) -> list[str]:
    return [a for a, (frm, _) in TRANSITIONS.items() if state in frm]


async def apply_feedback(session: AsyncSession, finding_id: str, *, action: str,
                         actor: str, reason: str | None, note: str | None,
                         via: str = "finding") -> dict[str, Any] | None:
    """One conditional UPDATE (race-safe: two presses, one wins). Returns
    {from_state, to_state, kind, lane, alarm_id, ...} or None when the row was
    not in an allowed state (the caller reads it back to say why)."""
    frm, to = TRANSITIONS[action]
    verdict_sql = {"acknowledge": "f.verdict", "dismiss": "'dismissed'",
                   "confirm": "'confirmed'", "reopen": "NULL"}[action]
    row = (await session.execute(text(f"""
        UPDATE anomaly_finding f
           SET state = CAST(:to AS text), verdict = {verdict_sql},
               acknowledged_at = CASE WHEN CAST(:action AS text) = 'acknowledge' THEN now()
                                      ELSE f.acknowledged_at END,
               acknowledged_by = CASE WHEN CAST(:action AS text) = 'acknowledge'
                                      THEN CAST(:actor AS text)
                                      ELSE f.acknowledged_by END,
               feedback = f.feedback || jsonb_build_array(jsonb_build_object(
                   'at', to_jsonb(now()), 'by', CAST(:actor AS text),
                   'action', CAST(:action AS text), 'reason', CAST(:reason AS text),
                   'note', CAST(:note AS text), 'from_state', f.state,
                   'to_state', CAST(:to AS text), 'detector_version', f.detector_version,
                   'via', CAST(:via AS text))),
               updated_at = now()
          FROM anomaly_finding old
         WHERE f.id = CAST(:id AS uuid) AND old.id = f.id AND f.state = ANY(:frm)
        RETURNING old.state AS from_state, f.state AS to_state, f.kind, f.lane,
                  f.alarm_id::text AS alarm_id, f.device_id::text AS device_id,
                  f.first_seen, f.detector_id
    """), {"id": finding_id, "to": to, "frm": list(frm), "action": action, "actor": actor,
           "reason": reason, "note": note, "via": via})).mappings().first()
    if row is None:
        return None
    out = dict(row)
    if action == "confirm" and out["kind"] == "process":
        await session.execute(text("""
            INSERT INTO anomaly_baseline_exclusion (device_id, starts_at, source, source_ref,
                                                    reason, created_by)
            VALUES (CAST(:dev AS uuid), :start, 'confirmed_finding', :ref, :reason, :by)
            ON CONFLICT (source, source_ref, device_id, room_id, datacenter_id)
               WHERE source_ref IS NOT NULL
            DO UPDATE SET ends_at = NULL
        """), {"dev": out["device_id"], "start": out["first_seen"], "ref": finding_id,
               "reason": f"confirmed {out['detector_id']} finding", "by": actor})
    return out


async def finding_state(session: AsyncSession, finding_id: str) -> str | None:
    return await session.scalar(text(
        "SELECT state FROM anomaly_finding WHERE id = CAST(:id AS uuid)"), {"id": finding_id})


async def detector_rows(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT id, version, layer, phase, name, diagnosis, device_types, primary_metric,
               required_metrics, default_lane, default_kind, alarm_type,
               severity::text AS severity, cadence_s, params, enabled, updated_at, updated_by
          FROM anomaly_detector ORDER BY id
    """))).mappings().all()
    return [dict(r) for r in rows]


async def detector_row(session: AsyncSession, detector_id: str) -> dict[str, Any] | None:
    for r in await detector_rows(session):
        if r["id"] == detector_id:
            return r
    return None


async def detector_stats(session: AsyncSession) -> dict[str, dict[str, int]]:
    """open / confirmed / dismissed per detector, verdicts at the current version."""
    rows = (await session.execute(text("""
        SELECT f.detector_id,
               count(*) FILTER (WHERE f.state <> 'resolved') AS open_findings,
               count(*) FILTER (WHERE f.verdict = 'confirmed'
                                  AND f.detector_version = d.version) AS confirmed,
               count(*) FILTER (WHERE f.verdict = 'dismissed'
                                  AND f.detector_version = d.version) AS dismissed
          FROM anomaly_finding f JOIN anomaly_detector d ON d.id = f.detector_id
         GROUP BY f.detector_id
    """))).mappings().all()
    return {r["detector_id"]: dict(r) for r in rows}


async def device_points(session: AsyncSession, device_types: list[str]
                        ) -> dict[str, tuple[str, list[tuple[str, str]]]]:
    """device id -> (type, the (metric, instance) series it delivers now).

    For the catalogue's devices_in_scope / inactive_devices counters: what
    has arrived in the last 15 min (numeric) or inside the boolean look-back.
    """
    if not device_types:
        return {}
    rows = (await session.execute(text("""
        SELECT d.id::text, d.device_type, m.key, s.instance
          FROM device d
          LEFT JOIN LATERAL (
              SELECT DISTINCT t.metric_id, t.instance FROM telemetry_sample t
               WHERE t.device_id = d.id AND t.ts > now() - interval '15 minutes'
              UNION
              SELECT DISTINCT b.metric_id, b.instance FROM telemetry_bool b
               WHERE b.device_id = d.id AND b.ts > now() - interval '30 minutes') s ON true
          LEFT JOIN metric m ON m.id = s.metric_id
         WHERE d.device_type = ANY(:types)
           AND d.lifecycle::text NOT IN ('decommissioned', 'retired')
    """), {"types": device_types})).all()
    out: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    for dev, dtype, key, inst in rows:
        entry = out.setdefault(dev, (dtype, []))
        if key is not None:
            entry[1].append((key, inst))
    return out


async def update_detector(session: AsyncSession, detector_id: str, *,
                          enabled: bool | None, params: dict[str, Any] | None,
                          bump: bool, actor: str) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        UPDATE anomaly_detector
           SET enabled = COALESCE(:enabled, enabled),
               params = params || COALESCE(CAST(:params AS jsonb), '{}'::jsonb),
               version = version + CASE WHEN :bump THEN 1 ELSE 0 END,
               updated_at = now(), updated_by = :actor
         WHERE id = :id
        RETURNING id, version
    """), {"id": detector_id, "enabled": enabled,
           "params": json.dumps(params) if params else None, "bump": bump,
           "actor": actor})).mappings().first()
    return dict(row) if row else None


async def open_findings_for(session: AsyncSession, detector_id: str,
                            device_id: str | None = None) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT id::text, device_id::text AS device_id, alarm_id::text AS alarm_id, instance
          FROM anomaly_finding
         WHERE detector_id = :det AND state <> 'resolved'
           AND (CAST(:dev AS uuid) IS NULL OR device_id = CAST(:dev AS uuid))
    """), {"det": detector_id, "dev": device_id})).mappings().all()
    return [dict(r) for r in rows]


async def list_overrides(session: AsyncSession, *, detector: str | None, device_id: str | None,
                         limit: int, offset: int, with_total: bool) -> dict[str, Any]:
    w, p = [], {"limit": limit, "offset": offset}
    if detector:
        w.append("o.detector_id = :det")
        p["det"] = detector
    if device_id:
        w.append("o.device_id = CAST(:dev AS uuid)")
        p["dev"] = device_id
    where = (" WHERE " + " AND ".join(w)) if w else ""
    rows = (await session.execute(text(f"""
        SELECT o.detector_id, o.device_id::text AS device_id, d.name AS device_name,
               o.enabled, COALESCE(o.params, '{{}}'::jsonb) AS params, o.reason,
               o.expires_at, o.created_by AS updated_by, o.updated_at
          FROM anomaly_override o JOIN device d ON d.id = o.device_id {where}
         ORDER BY o.updated_at DESC, o.detector_id, o.device_id
         LIMIT :limit OFFSET :offset
    """), p)).mappings().all()
    total = None
    if with_total:
        total = int(await session.scalar(text(
            f"SELECT count(*) FROM anomaly_override o {where}"), p) or 0)
    return {"items": [dict(r) for r in rows], "next_cursor": None, "total": total}


async def get_override(session: AsyncSession, detector_id: str,
                       device_id: str) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        SELECT o.detector_id, o.device_id::text AS device_id, d.name AS device_name,
               o.enabled, COALESCE(o.params, '{}'::jsonb) AS params, o.reason, o.expires_at,
               o.created_by AS updated_by, o.updated_at
          FROM anomaly_override o JOIN device d ON d.id = o.device_id
         WHERE o.detector_id = :det AND o.device_id = CAST(:dev AS uuid)
    """), {"det": detector_id, "dev": device_id})).mappings().first()
    return dict(row) if row else None


async def upsert_override(session: AsyncSession, detector_id: str, device_id: str, *,
                          params: dict[str, Any] | None, enabled: bool | None,
                          reason: str | None, expires_at: datetime | None,
                          actor: str) -> None:
    await session.execute(text("""
        INSERT INTO anomaly_override (detector_id, device_id, params, enabled, expires_at,
                                      reason, created_by)
        VALUES (:det, CAST(:dev AS uuid), CAST(:params AS jsonb), :enabled, :exp, :reason,
                :actor)
        ON CONFLICT (detector_id, device_id) DO UPDATE SET
            params = EXCLUDED.params, enabled = EXCLUDED.enabled,
            expires_at = EXCLUDED.expires_at, reason = EXCLUDED.reason,
            created_by = EXCLUDED.created_by, updated_at = now()
    """), {"det": detector_id, "dev": device_id,
           "params": json.dumps(params) if params is not None else None,
           "enabled": enabled, "exp": expires_at, "reason": reason, "actor": actor})


async def delete_override(session: AsyncSession, detector_id: str, device_id: str) -> bool:
    res = await session.execute(text("""
        DELETE FROM anomaly_override
         WHERE detector_id = :det AND device_id = CAST(:dev AS uuid)
    """), {"det": detector_id, "dev": device_id})
    return (res.rowcount or 0) > 0


async def device_exists(session: AsyncSession, device_id: str) -> bool:
    try:
        UUID(device_id)
    except ValueError:
        return False
    return bool(await session.scalar(text(
        "SELECT 1 FROM device WHERE id = CAST(:d AS uuid)"), {"d": device_id}))


_EXCLUSION_SELECT = """
    SELECT x.id::text AS id, x.device_id::text AS device_id, x.room_id::text AS room_id,
           x.datacenter_id::text AS datacenter_id, x.detector_id, m.key AS metric_key,
           x.starts_at, x.ends_at, x.source, x.source_ref, x.reason, x.created_by,
           x.created_at
      FROM anomaly_baseline_exclusion x
      LEFT JOIN metric m ON m.id = x.metric_id
"""


async def list_exclusions(session: AsyncSession, *, device_id=None, room=None, detector=None,
                          source=None, active=None, frm=None, to=None, limit=50, offset=0,
                          with_total=False) -> dict[str, Any]:
    w, p = [], {"limit": limit, "offset": offset}
    if device_id:
        w.append("x.device_id = CAST(:dev AS uuid)")
        p["dev"] = device_id
    if room:
        w.append("x.room_id = CAST(:room AS uuid)")
        p["room"] = room
    if detector:
        w.append("x.detector_id = :det")
        p["det"] = detector
    if source:
        w.append("x.source = ANY(:src)")
        p["src"] = list(source)
    if active is not None:
        a = "(x.starts_at <= now() AND (x.ends_at IS NULL OR x.ends_at > now()))"
        w.append(a if active else f"NOT {a}")
    if frm:
        w.append("(x.ends_at IS NULL OR x.ends_at > :frm)")
        p["frm"] = frm
    if to:
        w.append("x.starts_at < :to")
        p["to"] = to
    where = (" WHERE " + " AND ".join(w)) if w else ""
    rows = (await session.execute(text(
        _EXCLUSION_SELECT + where + " ORDER BY x.starts_at DESC, x.id LIMIT :limit OFFSET :offset"),
        p)).mappings().all()
    total = None
    if with_total:
        total = int(await session.scalar(text(
            "SELECT count(*) FROM anomaly_baseline_exclusion x" + where), p) or 0)
    return {"items": [dict(r) for r in rows], "next_cursor": None, "total": total}


async def get_exclusion(session: AsyncSession, exclusion_id: str) -> dict[str, Any] | None:
    try:
        UUID(exclusion_id)
    except ValueError:
        return None
    row = (await session.execute(text(_EXCLUSION_SELECT + " WHERE x.id = CAST(:id AS uuid)"),
                                 {"id": exclusion_id})).mappings().first()
    return dict(row) if row else None


async def create_exclusion(session: AsyncSession, body: dict[str, Any],
                           actor: str) -> tuple[dict[str, Any], bool]:
    """Insert, or return the existing row for a replayed (source, source_ref,
    scope). Returns (row, created)."""
    metric_id = None
    if body.get("metric_key"):
        metric_id = await session.scalar(text("SELECT id FROM metric WHERE key = :k"),
                                         {"k": body["metric_key"]})
    params = {"dev": body.get("device_id"), "room": body.get("room_id"),
              "dc": body.get("datacenter_id"), "det": body.get("detector_id"),
              "met": metric_id, "start": body["starts_at"], "end": body.get("ends_at"),
              "src": body["source"], "ref": body.get("source_ref"),
              "reason": body["reason"], "by": actor}
    new_id = await session.scalar(text("""
        INSERT INTO anomaly_baseline_exclusion (device_id, room_id, datacenter_id, detector_id,
                                                metric_id, starts_at, ends_at, source,
                                                source_ref, reason, created_by)
        VALUES (CAST(:dev AS uuid), CAST(:room AS uuid), CAST(:dc AS uuid), :det, :met,
                :start, :end, :src, :ref, :reason, :by)
        ON CONFLICT (source, source_ref, device_id, room_id, datacenter_id)
           WHERE source_ref IS NOT NULL
        DO NOTHING
        RETURNING id::text
    """), params)
    if new_id:
        return await get_exclusion(session, new_id), True
    existing = await session.scalar(text("""
        SELECT id::text FROM anomaly_baseline_exclusion
         WHERE source = :src AND source_ref = :ref
           AND device_id IS NOT DISTINCT FROM CAST(:dev AS uuid)
           AND room_id IS NOT DISTINCT FROM CAST(:room AS uuid)
           AND datacenter_id IS NOT DISTINCT FROM CAST(:dc AS uuid)
    """), params)
    return await get_exclusion(session, existing), False


async def delete_exclusion(session: AsyncSession, exclusion_id: str) -> bool:
    res = await session.execute(text(
        "DELETE FROM anomaly_baseline_exclusion WHERE id = CAST(:id AS uuid)"),
        {"id": exclusion_id})
    return (res.rowcount or 0) > 0


# --- nightly prune (docs/29 section 6.3) -----------------------------------------------

#: table, key column, predicate. Batched DELETE ... WHERE key IN (SELECT ...
#: LIMIT n) so no transaction holds a snapshot open for long on the tight host.
PRUNE_RULES: tuple[tuple[str, str, str], ...] = (
    ("anomaly_finding", "id",
     "state = 'resolved' AND resolved_at < CAST(:now AS timestamptz) - interval '2 years'"),
    ("anomaly_incident", "id",
     "state = 'resolved' AND resolved_at < CAST(:now AS timestamptz) - interval '2 years'"),
    ("anomaly_baseline", "id",
     "NOT is_current AND superseded_at < CAST(:now AS timestamptz) - interval '30 days'"),
    ("anomaly_state", "ctid", "updated_at < CAST(:now AS timestamptz) - interval '7 days'"),
    ("anomaly_baseline_exclusion", "id",
     "ends_at < CAST(:now AS timestamptz) - interval '2 years'"),
    ("anomaly_override", "ctid", "expires_at < CAST(:now AS timestamptz) - interval '7 days'"),
)


async def prune_batch(conn: Any, table: str, key: str, predicate: str, *, now: datetime,
                      batch: int) -> int:
    res = await conn.execute(text(f"""
        DELETE FROM {table} WHERE {key} IN (
            SELECT {key} FROM {table} WHERE {predicate} LIMIT :batch)
    """), {"now": now, "batch": batch})
    return res.rowcount or 0
