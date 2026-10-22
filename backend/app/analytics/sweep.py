"""The anomaly sweep, and the guards that make exactly one of it run.

Phase 0 ships the plumbing with nothing to score: `run_sweep` loads the
catalogue and returns. What is real already is everything around it, because
that is what has to be measured on the live host before a detector is added
(docs/33 task 0.16):

* **One sweep per interval, across replicas.** The two ingest replicas run
  unaligned monotonic timers. The lock holder is decided by
  `pg_try_advisory_xact_lock(ANOMALY_SWEEP_LOCK_KEY)` inside the sweep's own
  transaction, as the assigner does, and released at commit. The lock alone
  would still admit replica B sweeping 30 s after A, doubling the work and
  halving every dwell, so the holder then reads the `dcim:anomaly:sweep`
  heartbeat and skips when its `finished_at` is younger than 0.9 x
  DCIM_ANOMALY_SWEEP_S - the **cadence gate** (docs/29 section 10.1). A
  missing key means sweep: the lock still serialises.

* **The heartbeat is written while the lock is held**, before the caller
  commits. Written after commit, there is a window in which the other
  replica can take the released lock and read the previous, older
  heartbeat, and sweep a second time.

* **The nightly baseline fit** takes a SESSION-level
  `pg_try_advisory_lock(ANOMALY_BASELINE_LOCK_KEY)` on a dedicated
  connection, released in `finally`, because the fit spans many short
  per-batch transactions and one transaction held for minutes would pin the
  vacuum horizon on a host that already swaps. The once-per-UTC-day guard is
  `max(anomaly_baseline.fitted_at)`, in Postgres, so a Redis flush cannot
  trigger a second full fit.

Lock constants live in code, not env, beside the existing ones: assigner
"DCIM5", rollout "DCIM7", collector visibility "colv".
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.alarms.service import AlarmAction
from app.analytics import baselines
from app.analytics import window as win_store
from app.analytics.catalogue import (
    _SEVERITY_RANK,
    DERIVED_SEVERITY_CAP,
    L0_METRICS,
    Catalogue,
    Detector,
    cap_severity,
)
from app.analytics.detectors import quality
from app.analytics.detectors import rules as rl
from app.analytics.evidence import rule_evidence
from app.core import alert_taxonomy
from app.core.ashrae import dew_point_c
from app.core.logging import get_logger
from app.repositories import alarms as alarm_repo
from app.repositories import anomalies as repo

log = get_logger("anomaly")

#: "DCIMA" - the per-sweep transaction lock.
ANOMALY_SWEEP_LOCK_KEY = 0x44_43_49_4D_41
#: "DCIMB" - the nightly fit's session lock.
ANOMALY_BASELINE_LOCK_KEY = 0x44_43_49_4D_42

SWEEP_HEARTBEAT_KEY = "dcim:anomaly:sweep"
BASELINE_HEARTBEAT_KEY = "dcim:anomaly:baseline"
#: Not in docs/29 section 10's key table: the rolling record /health needs
#: for runs_1h, gate_skips_1h, errors_1h and p95_ms_1h (docs/30 section 11).
#: The heartbeat holds only the last sweep, and Prometheus counters are per
#: process, so neither can answer "how many sweeps ran in the last hour across
#: both replicas". One capped list, one compact entry per timer firing.
SWEEP_LOG_KEY = "dcim:anomaly:sweep:log"
SWEEP_LOG_MAX = 512
SWEEP_LOG_TTL_S = 7200

#: Skip when the last sweep finished less than this fraction of the cadence ago.
CADENCE_GATE_FRACTION = 0.9
#: The heartbeat outlives ten intervals, then /health reports "no sweep".
HEARTBEAT_TTL_SWEEPS = 10
BASELINE_HEARTBEAT_TTL_S = 48 * 3600

# Module tunables per docs/29 section 11 (in code, not env).
ACC_TTL_S = 86_400
PRUNE_BATCH = baselines.PRUNE_BATCH
BUNDLE_CACHE_TTL_S = 600


@dataclass
class SweepResult:
    ran: bool
    #: Why it did not run: 'lock_held' | 'cadence'.
    skipped: str | None = None
    series_scored: int = 0
    findings_opened: int = 0
    findings_resolved: int = 0
    alerts_raised: int = 0
    duration_ms: int = 0
    #: Alarm actions for the integration outbox and fanout (Phase 1).
    actions: list[Any] = field(default_factory=list)
    #: Frames for the `anomalies` websocket topic, at most 50 (Phase 1).
    frames: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FitResult:
    ran: bool
    skipped: str | None = None
    series_fitted: int = 0
    rejected: int = 0
    pruned_rows: int = 0


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# --- the sweep ---------------------------------------------------------------

#: Yield to ingest: above this pipeline lag the sweep does not run at all
#: (docs/33 task 1.3). DQ-1 would read a backlog as dropouts, and the sweep's
#: reads compete with the writes that are behind.
MAX_INGEST_LAG_S = 60.0
#: Finding frames per sweep on the `anomalies` topic, plus one summary. The
#: hub queues 256 frames per session and closes the session when it fills
#: (websocket/hub.py), so a burst of openings must not reach it whole.
MAX_FINDING_FRAMES = 50
#: Every derived raise goes through `derived_severity`; this counts guard trips.
_GUARD_TRIPS = {"n": 0}


def derived_severity(detector_severity: str | None,
                     cap: str = DERIVED_SEVERITY_CAP) -> str:
    """The severity a source="analytics" raise is allowed (docs/33 task 1.18).

    min(row, cap), then a hard guard: whatever a row or a future code path
    asks for, nothing above the cap leaves this function. MAJOR is reachable
    only by L5 escalating an incident root's EXISTING alarm (Phase 2, behind
    L5.major_enabled), never through this path.
    """
    sev = cap_severity(detector_severity or cap, cap)
    if _SEVERITY_RANK.get(sev, 99) > _SEVERITY_RANK[cap]:   # pragma: no cover - guard
        _GUARD_TRIPS["n"] += 1
        log.error("anomaly severity guard tripped", severity=sev, cap=cap)
        sev = cap
    return sev


async def run_sweep(session: AsyncSession, now: datetime, *, redis: Any = None,
                    lag_s: float | None = None) -> SweepResult:
    """One pass of L0 data quality and L2 rules over every in-scope device.

    In order: catalogue -> devices and gates -> incremental windows (Redis
    cursor, 30-min backfill on a miss) -> L0 per device (which masks) -> the
    room context -> L2 per device -> the dwell framework -> findings,
    derived alarms, clears -> alarm->finding acknowledgement sync -> the
    device rollup for every touched device -> capped frames. Everything runs
    in the caller's transaction; the caller enqueues the outbox in it and
    publishes after commit.
    """
    result = SweepResult(ran=True)
    cat = await repo.load_catalogue(session, now)
    engine = _Engine(session, cat, now, redis, result)
    await engine.run()
    return result


class _Engine:
    def __init__(self, session: AsyncSession, cat: Catalogue, now: datetime, redis: Any,
                 result: SweepResult) -> None:
        self.s, self.cat, self.now, self.result = session, cat, now, result
        self.ts = now.timestamp()
        self.win = win_store.HashStore(redis, win_store.WIN_KEY)
        self.acc = win_store.HashStore(redis, win_store.ACC_KEY)
        self.states: dict[str, rl.DwellState] = {}
        self.touched: set[str] = set()
        self.touches: list[dict[str, Any]] = []
        #: finding id -> (event, change, via); last write wins per finding.
        self.frames: dict[str, tuple[str, str | None, str | None]] = {}

    # --- setup --------------------------------------------------------------

    async def run(self) -> None:
        scorers = [d for d in self.cat.scorers() if d.layer in (0, 2)]
        types = sorted({t for d in scorers for t in d.device_types})
        devices = await repo.sweep_devices(self.s, types)
        self.open_rows = await repo.open_findings(self.s)
        if not devices and not self.open_rows:
            return
        self.mids = await repo.metric_ids(self.s)
        self.mkey = {v: k for k, v in self.mids.items()}
        self.open_by_key = {(r["detector_id"], r["device_id"], r["metric_id"], r["instance"]): r
                            for r in self.open_rows}
        await self.win.load()
        await self.acc.load()

        live = [d for d in devices if d["lifecycle"] not in repo.RETIRED_LIFECYCLES]
        views = await self._read(live, scorers)
        await self._resolve_retired_and_off(devices, views)

        # L0 first: it masks what L2 may read.
        verdicts: list[tuple[Detector, rl.DeviceView, dict, rl.Verdict]] = []
        for view in views.values():
            dq = {}
            for d in scorers:
                if d.layer != 0:
                    continue
                eff = self._effective(d, view)
                if eff is not None:
                    dq[d.id] = dict(eff.params)
            if not dq:
                continue
            for did, v in quality.evaluate(
                    view, dq, self.ts, lambda did, k, _v=view: self._state(did, _v, k),
                    L0_METRICS.get(view.device_type, ())):
                verdicts.append((self.cat.get(did), view, dq[did], v))

        ctx = self._context(views)
        for view in views.values():
            for d in scorers:
                if d.layer != 2 or d.id not in rl.RULES:
                    continue
                eff = self._effective(d, view)
                if eff is None:
                    continue
                params = dict(eff.params)
                ri = rl.RuleInput(
                    d.id, params, view, ctx, self.ts,
                    active=lambda k, _d=d.id, _v=view: self._state(_d, _v, k).active,
                    x=lambda k, _d=d.id, _v=view: self._state(_d, _v, k).x)
                for v in rl.run_rule(ri):
                    verdicts.append((d, view, params, v))

        self.result.series_scored = len(verdicts)
        for det, view, params, v in verdicts:
            await self._apply(det, view, params, v)

        await repo.touch_findings(self.s, self.touches, self.now)
        for fid in await repo.sync_alarm_acks(self.s, self.now):
            self.frames[fid] = ("anomaly.finding.updated", "acknowledged", "alarm")
        if self.touched:
            await alarm_repo.refresh_device_alarm_state(self.s, sorted(self.touched))
        for k, st in self.states.items():
            wire = st.to_wire()
            if wire == {"a": 0}:
                self.acc.delete(k)
            else:
                self.acc.put(k, json.dumps(wire, separators=(",", ":")))
        await self.win.flush()
        await self.acc.flush()
        self.result.frames = await self._build_frames()

    def _effective(self, d: Detector, view: rl.DeviceView):
        if view.device_type not in d.device_types:
            return None
        eff = self.cat.resolve(d.id, view.device_id, feature_enabled=True,
                               device_type=view.device_type, now=self.now,
                               available=view.available())
        return eff if eff is not None and eff.enabled else None

    # --- the reader (docs/33 task 1.3) --------------------------------------------

    async def _read(self, devices: list[dict], scorers: list[Detector]
                    ) -> dict[str, rl.DeviceView]:
        num_keys: set[str] = set()
        for t in {d["device_type"] for d in devices}:
            num_keys.update(L0_METRICS.get(t, ()))
        bool_keys = {"equipment_state"}
        for d in scorers:
            for spec in (*d.required_metrics, d.primary_metric):
                if spec == "*":
                    continue
                key = spec.split("{", 1)[0]
                (bool_keys if key == "equipment_state" else num_keys).add(key)
            for coupled in (d.params.get("coupled") or {}).values():
                num_keys.update(c[0] for c in coupled)
        num_keys.update(("inlet_temperature", "ambient_temperature", "relative_humidity",
                         "dew_point"))
        num_mids = sorted(self.mids[k] for k in num_keys if k in self.mids)
        bool_mids = sorted(self.mids[k] for k in bool_keys if k in self.mids)

        stored: dict[str, dict[str, Any]] = {}
        for f, raw in self.win.data.items():
            dev, _, rest = f.partition("|")
            stored.setdefault(dev, {})[rest] = raw
        views: dict[str, rl.DeviceView] = {}
        windows: dict[str, dict[str, Any]] = {}
        since_num: dict[str, datetime] = {}
        since_bool: dict[str, datetime] = {}
        for d in devices:
            did = d["device_id"]
            views[did] = rl.DeviceView(
                device_id=did, name=d["name"], device_type=d["device_type"],
                room_id=d["room_id"], datacenter_id=d["datacenter_id"],
                lifecycle=d["lifecycle"], online=bool(d["online"]),
                poll_interval_s=d["poll_interval_s"], maintenance=bool(d["maintenance"]),
                fire_predischarge=bool(d["fire_predischarge"]))
            ws = {rest: win_store.parse(raw) for rest, raw in stored.get(did, {}).items()}
            windows[did] = ws
            nums = [w.cursor for w in ws.values() if isinstance(w, win_store.NumWindow)]
            bools = [w.cursor for w in ws.values() if isinstance(w, win_store.BoolWindow)]
            since_num[did] = datetime.fromtimestamp(win_store.device_since(nums, self.ts), UTC)
            since_bool[did] = datetime.fromtimestamp(win_store.device_since(bools, self.ts),
                                                     UTC)
        floor = self.now - timedelta(seconds=win_store.BACKFILL_S)
        new_num: dict[tuple[str, str], list] = {}
        for dev, mid, inst, ts, v, q in await repo.read_numeric(
                self.s, since_num, num_mids, floor, self.now):
            new_num.setdefault((dev, f"{mid}|{inst}"), []).append((ts, v, q))
        new_bool: dict[tuple[str, str], list] = {}
        for dev, mid, inst, ts, v, q in await repo.read_bools(
                self.s, since_bool, bool_mids, floor, self.now):
            new_bool.setdefault((dev, f"{mid}|{inst}"), []).append((ts, bool(v), q))

        for did, view in views.items():
            ws = windows[did]
            rests = set(ws) | {r for (dv, r) in new_num if dv == did} \
                | {r for (dv, r) in new_bool if dv == did}
            for rest in rests:
                mid_s, _, inst = rest.partition("|")
                key_name = self.mkey.get(int(mid_s))
                if key_name is None:
                    continue
                field_ = f"{did}|{rest}"
                w = ws.get(rest)
                if (did, rest) in new_bool or isinstance(w, win_store.BoolWindow):
                    bw = w if isinstance(w, win_store.BoolWindow) else win_store.BoolWindow()
                    bw, got = win_store.merge_bool(bw, new_bool.get((did, rest), []), self.ts)
                    if bw.series.last_ts and self.ts - bw.series.last_ts > win_store.TTL_S:
                        self.win.delete(field_)
                        continue
                    view.bools[(key_name, inst)] = bw.series
                    if got:
                        view.fresh.add((key_name, inst))
                    self.win.put(field_, bw.to_wire())
                else:
                    nw = w if isinstance(w, win_store.NumWindow) else win_store.NumWindow()
                    nw, got = win_store.merge_numeric(nw, new_num.get((did, rest), []), self.ts)
                    if not nw.samples or self.ts - nw.samples[-1].ts > win_store.TTL_S:
                        self.win.delete(field_)
                        continue
                    view.num[(key_name, inst)] = nw.samples
                    if got:
                        view.fresh.add((key_name, inst))
                    self.win.put(field_, nw.to_wire())
        return views

    def _context(self, views: dict[str, rl.DeviceView]) -> rl.Context:
        ctx = rl.Context()
        dew: dict[str, list[float]] = {}
        for v in views.values():
            if not v.room_id:
                continue
            if v.device_type in ("sensor", "pdu"):
                for k in v.instances("inlet_temperature"):
                    x = None if k in v.masked else v.last(k, self.ts)
                    if x is not None:
                        ctx.room_inlet.setdefault(v.room_id, []).append(x)
            if v.device_type == "sensor":
                dp = v.last(v.pick("dew_point"), self.ts)
                if dp is None:
                    t = v.last(v.pick("ambient_temperature"), self.ts)
                    rh = v.last(v.pick("relative_humidity"), self.ts)
                    if t is not None and rh is not None:
                        dp = dew_point_c(t, rh)
                if dp is not None:
                    dew.setdefault(v.room_id, []).append(dp)
        ctx.room_dew_max = {r: max(vs) for r, vs in dew.items()}
        return ctx

    # --- state ------------------------------------------------------------------

    def _finding_instance(self, det: Detector, metric_key: str, instance: str) -> str:
        return alert_taxonomy.alarm_instance(metric_key, instance, det.alarm_type)

    def _state(self, detector_id: str, view: rl.DeviceView, key: rl.Key) -> rl.DwellState:
        det = self.cat.get(detector_id)
        mid = self.mids.get(key[0])
        inst = self._finding_instance(det, key[0], key[1]) if det else key[1]
        f = f"{detector_id}|{view.device_id}|{mid}|{inst}"
        st = self.states.get(f)
        if st is None:
            raw = self.acc.get(f)
            try:
                st = rl.DwellState.from_wire(json.loads(raw) if raw else None)
            except (TypeError, ValueError):
                st = rl.DwellState()
            # The database is the truth about what is open. A Redis loss
            # must not orphan an open finding (it could never resolve), and
            # a finding resolved by the API (a mute, a disable) must not be
            # held active here.
            row = self.open_by_key.get((detector_id, view.device_id, mid, inst))
            if row is not None and not st.active:
                st.active, st.pending_since = True, None
                st.first_seen = row["first_seen"].timestamp()
            elif row is None and st.active:
                st.active, st.clear_since = False, None
            if row is not None:
                st.reopens = int(row["reopen_count"])
            self.states[f] = st
        return st

    # --- lifecycle --------------------------------------------------------------

    async def _resolve_retired_and_off(self, devices: list[dict],
                                       views: dict[str, rl.DeviceView]) -> None:
        """System resolutions the API may not have made: retired devices,
        disabled detectors, muted devices (docs/29 section 8.1)."""
        by_id = {d["device_id"]: d for d in devices}
        for row in list(self.open_rows):
            det = self.cat.get(row["detector_id"])
            dev = by_id.get(row["device_id"])
            reason = None
            if det is None or not det.enabled:
                reason = "detector_disabled"
            elif dev is not None and dev["lifecycle"] in repo.RETIRED_LIFECYCLES:
                reason = "device_retired"
            else:
                ov = self.cat.overrides.get((row["detector_id"], row["device_id"]))
                if ov is not None and ov.active(self.now) and ov.enabled is False:
                    reason = "muted"
            if reason:
                await self._resolve(det, row, reason)
                self.open_by_key.pop((row["detector_id"], row["device_id"], row["metric_id"],
                                      row["instance"]), None)

    async def _resolve(self, det: Detector | None, row: dict, resolution: str) -> None:
        done = await repo.resolve_finding(self.s, row["id"], resolution=resolution,
                                          at=self.now)
        if done is None:
            return
        self.result.findings_resolved += 1
        self.frames[row["id"]] = ("anomaly.finding.resolved", None, None)
        if done["alarm_id"] and det is not None and det.alarm_type:
            for c in await alarm_repo.clear_alarms(
                    self.s, device_id=done["device_id"], alarm_types=[det.alarm_type],
                    instance=done["instance"], at=self.now, by="analytics"):
                self.touched.add(done["device_id"])
                await alarm_repo.record_history(
                    self.s, alarm_id=c["id"], device_id=done["device_id"], action="cleared",
                    severity=c["severity"], actor="analytics",
                    detail={"finding_id": row["id"], "resolution": resolution})
                self.result.actions.append(AlarmAction("alarm_cleared", c))

    async def _apply(self, det: Detector, view: rl.DeviceView, params: dict,
                     v: rl.Verdict) -> None:
        mid = self.mids.get(v.metric_key)
        if mid is None:
            return
        inst = self._finding_instance(det, v.metric_key, v.instance)
        st = self._state(det.id, view, (v.metric_key, v.instance))
        event = rl.step(st, v, params, self.ts)
        row = self.open_by_key.get((det.id, view.device_id, mid, inst))
        if event == rl.TOUCH and row is not None:
            self.touches.append({"id": row["id"], "score": rl.finite(v.score),
                                 "observed": rl.finite(v.observed)})
        elif event == rl.RESOLVE and row is not None:
            await self._resolve(det, row, "cleared")
            self.open_by_key.pop((det.id, view.device_id, mid, inst), None)
        elif event == rl.OPEN or (event == rl.TOUCH and row is None):
            await self._open(det, view, params, v, st, mid, inst)

    async def _open(self, det: Detector, view: rl.DeviceView, params: dict, v: rl.Verdict,
                    st: rl.DwellState, mid: int, inst: str) -> None:
        first = st.first_seen or self.ts
        evidence = rule_evidence(detector_id=det.id, detector_version=det.version,
                                 params=params, verdict=v, dev=view, now=self.ts,
                                 first_seen=first,
                                 window_s=float(params.get("window_s", 1800)))
        lane = det.default_lane
        kind = "sensor" if det.layer == 0 else det.default_kind
        out = await repo.open_finding(
            self.s, detector_id=det.id, detector_version=det.version, layer=det.layer,
            lane=lane, kind=kind, device_id=view.device_id, metric_id=mid, instance=inst,
            datacenter_id=view.datacenter_id, room_id=view.room_id,
            first_seen=datetime.fromtimestamp(first, UTC), now=self.now,
            reopen_window_s=int(params.get("reopen_window_s", 1800)),
            score=rl.finite(v.score), expected=rl.finite(v.expected),
            observed=rl.finite(v.observed), summary=v.summary or det.name,
            evidence=evidence)
        st.reopens = int(out["reopen_count"])
        self.open_by_key[(det.id, view.device_id, mid, inst)] = {
            "id": out["id"], "alarm_id": out["alarm_id"], "reopen_count": out["reopen_count"],
            "first_seen": datetime.fromtimestamp(first, UTC)}
        if out["outcome"] != "existing":
            self.result.findings_opened += 1
            self.frames[out["id"]] = ("anomaly.finding.opened", None, None)
        if lane == "alert" and det.alarm_type:
            await self._raise(det, view, v, out["id"], inst)

    async def _raise(self, det: Detector, view: rl.DeviceView, v: rl.Verdict,
                     finding_id: str, inst: str) -> None:
        """docs/29 section 9.2: one derived alarm per alert-lane finding."""
        metric_key = v.alarm_metric_key
        if metric_key in ("equipment_state", "alarm_state"):   # docs/29 D14
            metric_key = None
        alarm = await alarm_repo.raise_alarm(
            self.s, device_id=view.device_id, alarm_type=det.alarm_type, instance=inst,
            severity=derived_severity(det.severity), message=v.summary or det.name,
            source="analytics", observed_at=self.now, metric_key=metric_key,
            value=rl.finite(v.observed), threshold=rl.finite(v.threshold),
            category=alert_taxonomy.BY_ALARM_TYPE.get(det.alarm_type),
            attributes={"anomaly": {
                "finding_id": finding_id, "detector_id": det.id,
                "detector_version": det.version, "diagnosis": v.diagnosis,
                "expected": rl.finite(v.expected), "observed": rl.finite(v.observed),
                "threshold": rl.finite(v.threshold)}})
        if alarm is None:
            return
        await repo.link_alarm(self.s, finding_id, alarm["id"])
        if alarm["change"] == "touched":
            return
        self.touched.add(view.device_id)
        if alarm["change"] == "created":
            self.result.alerts_raised += 1
            await alarm_repo.record_history(
                self.s, alarm_id=alarm["id"], device_id=view.device_id, action="raised",
                severity=alarm["severity"], actor="analytics",
                detail={"finding_id": finding_id, "detector_id": det.id})
            self.result.actions.append(AlarmAction("alarm_created", alarm))
        else:
            self.result.actions.append(AlarmAction("alarm_updated", alarm))

    # --- frames -----------------------------------------------------------------

    async def _build_frames(self) -> list[dict[str, Any]]:
        return await build_finding_frames(self.s, self.frames)


async def resolve_findings(session: AsyncSession, rows: list[dict[str, Any]], *,
                           alarm_type: str | None, resolution: str, at: datetime,
                           by: str = "system") -> tuple[list[AlarmAction], dict[str, tuple]]:
    """Resolve findings outside a sweep (a PATCH disable, a mute override) and
    clear their derived alarms, exactly as the sweep would. Returns the alarm
    actions (outbox + fanout) and the frame changes for build_finding_frames.
    """
    actions: list[AlarmAction] = []
    changes: dict[str, tuple] = {}
    touched: set[str] = set()
    for row in rows:
        done = await repo.resolve_finding(session, row["id"], resolution=resolution,
                                          at=at, by=by)
        if done is None:
            continue
        changes[row["id"]] = ("anomaly.finding.resolved", None, None)
        if done["alarm_id"] and alarm_type:
            for c in await alarm_repo.clear_alarms(
                    session, device_id=done["device_id"], alarm_types=[alarm_type],
                    instance=done["instance"], at=at, by="analytics"):
                touched.add(done["device_id"])
                await alarm_repo.record_history(
                    session, alarm_id=c["id"], device_id=done["device_id"],
                    action="cleared", severity=c["severity"], actor=by,
                    detail={"finding_id": row["id"], "resolution": resolution})
                actions.append(AlarmAction("alarm_cleared", c))
    if touched:
        await alarm_repo.refresh_device_alarm_state(session, sorted(touched))
    return actions, changes


async def build_finding_frames(session: AsyncSession,
                               changes: dict[str, tuple[str, str | None, str | None]],
                               cap: int = MAX_FINDING_FRAMES) -> list[dict[str, Any]]:
    """At most `cap` finding frames plus one `bulk` summary (docs/30 section 8).

    One frame per finding (the dict key), openings and resolutions first.
    The frame carries the section 4.1 row, read back inside the transaction
    so it matches what REST returns once committed.
    """
    if not changes:
        return []
    order = {"anomaly.finding.opened": 0, "anomaly.finding.resolved": 1,
             "anomaly.finding.updated": 2}
    ids = sorted(changes, key=lambda i: (order.get(changes[i][0], 9), i))
    keep, dropped = ids[:cap], len(ids) - cap
    rows = {r["id"]: r for r in await repo.findings_by_ids(session, keep)}
    frames = []
    for fid in keep:
        if fid not in rows:
            continue
        event, change, via = changes[fid]
        frame: dict[str, Any] = {"event": event}
        if change:
            frame["change"] = change
        if via:
            frame["via"] = via
        frame["finding"] = rows[fid]
        frames.append(frame)
    if dropped > 0:
        frames.append({"event": "anomaly.finding.updated", "change": "bulk",
                       "count": dropped})
    return frames


async def try_sweep_lock(session: AsyncSession) -> bool:
    return bool((await session.execute(
        text("SELECT pg_try_advisory_xact_lock(:key)"),
        {"key": ANOMALY_SWEEP_LOCK_KEY})).scalar())


async def read_heartbeat(redis: Any, key: str = SWEEP_HEARTBEAT_KEY) -> dict[str, Any] | None:
    raw = await redis.get(key)
    if not raw:
        return None
    try:
        hb = json.loads(raw)
    except ValueError:
        return None
    return hb if isinstance(hb, dict) else None


def cadence_gate_closed(heartbeat: dict[str, Any] | None, now: datetime,
                        sweep_s: int) -> bool:
    """True when the last sweep finished under 0.9 x sweep_s ago.

    A heartbeat from the future (clock skew between replicas) also closes the
    gate: skipping one interval is cheaper than sweeping twice.
    """
    if not heartbeat:
        return False
    finished = _parse(heartbeat.get("finished_at"))
    if finished is None:
        return False
    return (now - finished).total_seconds() < CADENCE_GATE_FRACTION * sweep_s


async def write_heartbeat(redis: Any, *, consumer: str, started_at: datetime,
                          finished_at: datetime, result: SweepResult,
                          sweep_s: int, error: str | None = None) -> None:
    payload = {
        "consumer": consumer, "started_at": _iso(started_at),
        "finished_at": _iso(finished_at), "duration_ms": result.duration_ms,
        "series_scored": result.series_scored,
        "findings_opened": result.findings_opened,
        "findings_resolved": result.findings_resolved,
        "alerts_raised": result.alerts_raised, "error": error,
    }
    await redis.set(SWEEP_HEARTBEAT_KEY, json.dumps(payload),
                    ex=HEARTBEAT_TTL_SWEEPS * sweep_s)


async def record_outcome(redis: Any, *, consumer: str, outcome: str, at: datetime,
                         duration_ms: int | None = None, error: str | None = None) -> None:
    """Append to the rolling sweep log /health reads. Best effort."""
    entry = {"at": at.timestamp(), "consumer": consumer, "outcome": outcome}
    if duration_ms is not None:
        entry["ms"] = duration_ms
    if error:
        entry["error"] = error[:200]
    try:
        await redis.lpush(SWEEP_LOG_KEY, json.dumps(entry))
        await redis.ltrim(SWEEP_LOG_KEY, 0, SWEEP_LOG_MAX - 1)
        await redis.expire(SWEEP_LOG_KEY, SWEEP_LOG_TTL_S)
    except Exception as exc:  # the sweep's outcome does not depend on its log
        log.warning("anomaly sweep log write failed", error=str(exc))


async def read_outcomes(redis: Any, now: datetime, window_s: int = 3600) -> list[dict]:
    raw = await redis.lrange(SWEEP_LOG_KEY, 0, SWEEP_LOG_MAX - 1)
    cutoff = now.timestamp() - window_s
    out = []
    for item in raw or ():
        try:
            e = json.loads(item)
        except (TypeError, ValueError):
            continue
        if isinstance(e, dict) and float(e.get("at", 0)) >= cutoff:
            out.append(e)
    return out


SweepFn = Callable[[AsyncSession, datetime], Awaitable[SweepResult]]


async def guarded_sweep(session: AsyncSession, redis: Any, *, consumer: str,
                        sweep_s: int, now: datetime | None = None,
                        sweep: SweepFn | None = None,
                        lag_s: float | None = None) -> SweepResult:
    """Yield, lock, gate, sweep, heartbeat - in the caller's transaction.

    The caller owns the transaction (`unit_of_work()`): commit releases the
    lock, and the outbox rows the sweep enqueues commit with its writes.

    `lag_s` is this worker's last measured ingest lag. Above
    MAX_INGEST_LAG_S the sweep yields before taking the lock, so the other
    replica - or the next interval - sweeps instead (docs/33 task 1.3).
    """
    now = now or datetime.now(UTC)
    if lag_s is not None and lag_s > MAX_INGEST_LAG_S:
        return SweepResult(ran=False, skipped="lag")
    if not await try_sweep_lock(session):
        return SweepResult(ran=False, skipped="lock_held")
    if cadence_gate_closed(await read_heartbeat(redis), now, sweep_s):
        return SweepResult(ran=False, skipped="cadence")
    t0 = time.perf_counter()
    if sweep is not None:
        result = await sweep(session, now)
    else:
        result = await run_sweep(session, now, redis=redis, lag_s=lag_s)
    result.duration_ms = int((time.perf_counter() - t0) * 1000)
    # `now` is the sweep's logical time; finished_at must be comparable with
    # the next caller's `now`, so it is advanced by the measured duration
    # rather than read from a second clock.
    finished = now + timedelta(milliseconds=result.duration_ms)
    await write_heartbeat(redis, consumer=consumer, started_at=now, finished_at=finished,
                          result=result, sweep_s=sweep_s)
    return result


# --- the nightly fit ---------------------------------------------------------

async def run_baseline_fit(conn: AsyncConnection, now: datetime) -> FitResult:
    """Fit every L3/L4 series, then prune.

    Phase 1: nothing to fit yet (Phase 3 fills that in with per-batch
    transactions on `conn`); the nightly batched prune of docs/29 section 6.3
    runs here, under the DCIMB session lock (docs/33 task 1.15).
    """
    pruned = await baselines.prune(conn, now)
    return FitResult(ran=True, pruned_rows=sum(pruned.values()))


FitFn = Callable[[AsyncConnection, datetime], Awaitable[FitResult]]


async def guarded_fit(conn: AsyncConnection, redis: Any, *, consumer: str,
                      hour_utc: int, now: datetime | None = None,
                      fit: FitFn | None = None) -> FitResult:
    """Session lock, daily guard, fit, unlock - on a dedicated connection.

    `conn` must not be shared with anything else: a session-level advisory
    lock belongs to the connection, and a pooled connection handed back with
    the lock still held would make the next borrower the holder.
    """
    now = now or datetime.now(UTC)
    got = bool((await conn.execute(text("SELECT pg_try_advisory_lock(:key)"),
                                   {"key": ANOMALY_BASELINE_LOCK_KEY})).scalar())
    # Close the implicit transaction at once: the lock is session-level and
    # survives it, and an idle-in-transaction connection pins the horizon.
    await conn.commit()
    if not got:
        return FitResult(ran=False, skipped="lock_held")
    try:
        since = repo.fit_day_start(now, hour_utc)
        last = (await conn.execute(text("SELECT max(fitted_at) FROM anomaly_baseline"))).scalar()
        await conn.commit()
        if last is not None and last >= since:
            return FitResult(ran=False, skipped="already_fitted")
        # Secondary, Redis-side: a pass that fitted nothing (every Phase 0
        # pass, and a Phase 3 night with no eligible series) writes no
        # baseline row, so Postgres alone would re-run it on every check.
        # Losing this key only costs one repeat of that empty pass.
        hb = await read_heartbeat(redis, BASELINE_HEARTBEAT_KEY)
        if hb and hb.get("fit_date") == since.date().isoformat():
            return FitResult(ran=False, skipped="already_fitted")
        started = datetime.now(UTC)
        result = await (fit or run_baseline_fit)(conn, now)
        finished = datetime.now(UTC)
        payload = {
            "consumer": consumer, "fit_date": since.date().isoformat(),
            "started_at": _iso(started), "finished_at": _iso(finished),
            "series_fitted": result.series_fitted, "rejected": result.rejected,
            "pruned_rows": result.pruned_rows,
        }
        await redis.set(BASELINE_HEARTBEAT_KEY, json.dumps(payload),
                        ex=BASELINE_HEARTBEAT_TTL_S)
        return result
    finally:
        try:
            await conn.rollback()
            await conn.execute(text("SELECT pg_advisory_unlock(:key)"),
                               {"key": ANOMALY_BASELINE_LOCK_KEY})
            await conn.commit()
        except Exception as exc:
            # Closing the connection (the caller's context manager) releases a
            # session lock regardless; log and let that happen.
            log.warning("baseline lock release failed", error=str(exc))
