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
