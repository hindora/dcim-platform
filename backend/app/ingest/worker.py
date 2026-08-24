"""Ingest worker: the sole writer to PostgreSQL and TimescaleDB.

    consume -> enrich -> derive rates -> COPY -> upsert state -> publish -> ack

The ordering is deliberate: commit BEFORE publishing (so the UI never sees a
value that rolls back) and ack AFTER publishing (so a crash redelivers rather
than loses). Every sink is idempotent, which is what makes at-least-once safe.

Run at least two replicas; entries left pending by a dead worker are reclaimed
with XAUTOCLAIM.

    python -m app.ingest.worker
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import time
from datetime import UTC, datetime

import msgpack
from redis.asyncio import Redis
from sqlalchemy import text

from app.alarms import platform_monitor
from app.alarms.service import AlarmService, event_row
from app.contracts.messages_gen import (
    CollectorHeartbeat,
    EndpointState,
    EventBatch,
    Quality,
    Severity,
    Stream,
    TelemetryBatch,
    ValueType,
    ts_to_dt,
)
from app.core import metrics
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.metrics_gen import METRICS
from app.db.session import dispose_engine, unit_of_work
from app.ingest import rates, writer
from app.ingest.enrich import InventoryCache
from app.ingest.fanout import Fanout
from app.repositories import alarms as repo_alarms

log = get_logger("ingest")

# How often to look for endpoints that answer but say nothing. Well under
# the smallest grace period, so an endpoint crosses its threshold and is
# alarmed within about a minute of doing so.
STALENESS_SWEEP_S = 60.0

# How often the platform evaluates its own health. Frequent enough that a dead
# collector is noticed inside a minute, rare enough that it is not a load.
PLATFORM_CHECK_S = 30.0

_QUALITY_NAMES = {int(q): q.name.lower() for q in Quality}
_SEVERITY_NAMES = {int(s): s.name for s in Severity}
_COMM_STATUS = {1: "ONLINE", 2: "DEGRADED", 3: "OFFLINE", 4: "UNKNOWN", 5: "DISABLED"}

# Hot metrics that have a dedicated column on device_state.
_HOT_COLUMNS = {
    "power_draw": "power_w",
    "inlet_temperature": "inlet_temp_c",
    "cpu_utilization": "cpu_util_pct",
    "relative_humidity": "humidity_pct",
}


class IngestWorker:
    def __init__(self, consumer_name: str | None = None) -> None:
        self.settings = get_settings()
        self.consumer = consumer_name or f"{socket.gethostname()}-{os.getpid()}"
        # -inf so the first tick sweeps immediately rather than after a minute.
        self._last_staleness_sweep = float("-inf")
        self._last_platform_check = float("-inf")
        # Pipeline latency measured on the last telemetry batch. None until a
        # batch has actually been seen - an idle worker has no lag to report,
        # and reporting zero would be a claim it cannot support.
        self._last_lag_s: float | None = None
        self._batches = 0
        self._samples = 0
        self.redis: Redis = Redis.from_url(self.settings.redis_url)
        self.cache = InventoryCache()
        self.fanout = Fanout(self.redis)
        self.alarms = AlarmService(self.redis)
        self._stop = asyncio.Event()

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        await self._ensure_groups()
        async with unit_of_work() as session:
            await self.cache.refresh(session)
        log.info("ingest worker started", consumer=self.consumer)

        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("ingest tick failed", error=str(exc), exc_info=True)
                await asyncio.sleep(1.0)

        await self.redis.aclose()
        await dispose_engine()
        log.info("ingest worker stopped")

    def stop(self) -> None:
        self._stop.set()

    async def _ensure_groups(self) -> None:
        for stream in (Stream.TELEMETRY, Stream.EVENTS, Stream.ENDPOINTSTATE,
                       Stream.HEARTBEAT):
            try:
                await self.redis.xgroup_create(stream, self.settings.ingest_group,
                                               id="0", mkstream=True)
                log.info("created consumer group", stream=stream)
            except Exception as exc:  # BUSYGROUP means it already exists
                if "BUSYGROUP" not in str(exc):
                    raise

    # ------------------------------------------------------------------ loop

    async def _tick(self) -> None:
        if self.cache.is_stale():
            async with unit_of_work() as session:
                await self.cache.refresh(session)

        await self._maybe_sweep_staleness()

        # Before the early return below: the worker is alive whether or not
        # anything arrived, and an idle pipeline must not look like a dead one.
        await self._heartbeat_and_monitor()

        await self._reclaim_stale()

        entries = await self.redis.xreadgroup(
            self.settings.ingest_group, self.consumer,
            {Stream.TELEMETRY: ">", Stream.EVENTS: ">",
             Stream.ENDPOINTSTATE: ">", Stream.HEARTBEAT: ">"},
            count=self.settings.ingest_batch_size,
            block=self.settings.ingest_block_ms,
        )
        if not entries:
            return

        for stream_name, messages in entries:
            name = stream_name.decode() if isinstance(stream_name, bytes) else stream_name
            ids = [mid for mid, _ in messages]
            payloads = [msgpack.unpackb(fields[b"p"], raw=False) for _, fields in messages]

            metrics.ingest_messages.labels(stream=name, result="consumed").inc(
                len(payloads))

            if name == Stream.TELEMETRY:
                self._measure_lag(payloads)
                self._trace(payloads, name)
                await self._handle_telemetry(payloads)
            elif name == Stream.EVENTS:
                await self._handle_events(payloads)
            elif name == Stream.ENDPOINTSTATE:
                await self._handle_endpoint_state(payloads)
            elif name == Stream.HEARTBEAT:
                await self._handle_heartbeat(payloads)

            await self.redis.xack(name, self.settings.ingest_group, *ids)

    def _trace(self, payloads: list[dict], stream: str) -> None:
        """Carry the batch's trace id into this stage's logs.

        The contract has carried ``trace_id`` on every batch since the schema
        was written and nothing has ever read or set it. Logging it here closes
        the consuming half: once a batch arrives with an id, the write, the
        alarm evaluation and the websocket publish that follow it can all be
        found by that id, which is what turns "why did this alarm take forty
        seconds to appear" into a query rather than a hypothesis.

        The producing half is not done: the Go collector still sends an empty
        trace_id, so in practice this logs nothing today. It is written to the
        W3C shape so that an OpenTelemetry exporter is a small later step - and
        no exporter is wired here deliberately, because there is no tracing
        backend in this deployment to send spans to, and shipping an untestable
        exporter would be worse than shipping none.
        """
        traced = [raw.get("trace_id") for raw in payloads if raw.get("trace_id")]
        if not traced:
            return
        log.debug("batch traced", stream=stream, trace_id=traced[0],
                  batches=len(payloads), consumer=self.consumer)

    def _measure_lag(self, payloads: list[dict]) -> None:
        """Pipeline latency: collector publish -> here.

        Measured from ``sent_at`` on the batch, which is the only timestamp that
        isolates transport and queueing from the device's own poll cadence.
        ``collected_at`` would fold the poll interval into the number, so a
        fleet polled every 120 s would show two minutes of "lag" while perfectly
        healthy - and the 60 s threshold in the spec would be permanently
        breached by a system with nothing wrong.

        The newest batch in the read is used rather than the oldest: with a
        backlog the oldest entry is late by definition, and what is wanted is
        how far behind the pipeline is now.
        """
        sent = [raw.get("sent_at", 0) for raw in payloads if raw.get("sent_at")]
        if not sent:
            return
        newest_s = max(sent) / 1_000_000.0
        lag = time.time() - newest_s
        # A collector whose clock is ahead produces a negative lag. That is a
        # clock problem, not a negative latency, and clamping keeps it from
        # masking a real backlog on another collector.
        lag = max(0.0, lag)
        self._last_lag_s = lag
        self._batches += len(payloads)
        metrics.ingest_lag.labels(stream=Stream.TELEMETRY).set(lag)
        metrics.ingest_lag_hist.labels(stream=Stream.TELEMETRY).observe(lag)

    async def _heartbeat_and_monitor(self) -> None:
        """Say the worker is alive, then judge the platform.

        The heartbeat is written every tick and the evaluation runs on a timer:
        liveness has to be cheap and frequent, while the evaluation touches the
        database and does not need to.
        """
        try:
            await platform_monitor.write_heartbeat(
                self.redis, consumer=self.consumer, lag_s=self._last_lag_s,
                batches=self._batches, samples=self._samples)
        except Exception as exc:
            log.error("heartbeat write failed", error=str(exc))

        now = time.monotonic()
        if now - self._last_platform_check < PLATFORM_CHECK_S:
            return
        self._last_platform_check = now
        try:
            async with unit_of_work() as session:
                await platform_monitor.run_once(
                    session, self.redis,
                    streams=[Stream.TELEMETRY, Stream.EVENTS],
                    group=self.settings.ingest_group,
                    ingest_lag_s=self._last_lag_s)
        except Exception as exc:
            # Self-monitoring must never be the thing that stops ingestion.
            log.error("platform monitor failed", error=str(exc), exc_info=True)

    async def _maybe_sweep_staleness(self) -> None:
        """Look for endpoints that answer but deliver nothing.

        On a timer rather than in the message path: the condition is the
        absence of messages, so nothing will ever arrive to prompt the check.
        """
        now = time.monotonic()
        if now - self._last_staleness_sweep < STALENESS_SWEEP_S:
            return
        self._last_staleness_sweep = now
        try:
            async with unit_of_work() as session:
                actions = await self.alarms.sweep_staleness(session)
                # Same timer, because it is the same kind of question: an alarm
                # nothing will ever come back to clear.
                actions += await self.alarms.sweep_dead_endpoints(session)
            for action in actions:
                await self.fanout.alarm(action.kind, action.alarm)
        except Exception as exc:
            # A failed sweep must not stop telemetry ingestion; it runs again
            # on the next interval.
            log.error("staleness sweep failed", error=str(exc), exc_info=True)

    async def _reclaim_stale(self) -> None:
        """Take over entries a dead worker never acked."""
        for stream in (Stream.TELEMETRY, Stream.EVENTS, Stream.ENDPOINTSTATE,
                       Stream.HEARTBEAT):
            with contextlib.suppress(Exception):
                await self.redis.xautoclaim(
                    stream, self.settings.ingest_group, self.consumer,
                    min_idle_time=self.settings.ingest_claim_idle_ms, count=100)

    # -------------------------------------------------------------- handlers

    async def _handle_telemetry(self, payloads: list[dict]) -> None:
        samples = []
        for raw in payloads:
            batch = TelemetryBatch.from_dict(raw)
            samples.extend(batch.samples)
        if not samples:
            return

        sample_rows: list[writer.SampleRow] = []
        bool_rows: list[writer.BoolRow] = []
        text_rows: list[writer.TextRow] = []
        # Gauge samples the threshold rules get to see. Counters are excluded:
        # a rule on a raw counter would compare an ever-growing number against a
        # fixed limit and fire once, forever.
        rule_inputs: list[dict] = []
        alarm_actions = []
        hot: dict[str, writer.HotUpdate] = {}
        ws_frames: dict[str, dict] = {}
        unknown_metrics: set[str] = set()
        unresolved_interfaces: set[str] = set()
        # endpoint id -> newest observation in this batch.
        produced: dict[str, datetime] = {}

        # Counter baselines live in Redis so a worker restart does not lose them
        # and a decommissioned endpoint expires by itself.
        baseline_reads = {}
        for s in samples:
            if s.value_type == int(ValueType.COUNTER):
                baseline_reads[rates.baseline_key(s.endpoint_id, s.metric, s.instance)] = s
        baselines = await self._load_baselines(list(baseline_reads))
        baseline_writes: dict[str, str] = {}

        async with unit_of_work() as session:
            for s in samples:
                definition = METRICS.get(s.metric)
                if definition is None:
                    unknown_metrics.add(s.metric)
                    continue
                metric_id = self.cache.metric_id(s.metric)
                if metric_id is None:
                    unknown_metrics.add(s.metric)
                    continue

                ctx = await self.cache.device(s.device_id, session)
                if ctx is None:
                    continue  # already logged and counted by the cache

                if definition.group == "interfaces" and s.instance:
                    # One port, one series. The collector has already expanded
                    # short forms, but only inventory knows what the port is
                    # actually called - and an agent indexing by ifIndex sends
                    # a bare number that means nothing on its own.
                    #
                    # An instance that does not resolve is kept, not dropped: a
                    # port inventory has not caught up with is still carrying
                    # traffic, and losing it would hide exactly the interface
                    # someone just patched.
                    canonical = self.cache.canonical_interface(s.device_id, s.instance)
                    if canonical is None:
                        unresolved_interfaces.add(f"{ctx.name}:{s.instance}")
                    elif canonical != s.instance:
                        s.instance = canonical

                observed = ts_to_dt(s.observed_at) or ts_to_dt(s.collected_at) \
                    or datetime.now(UTC)
                quality = _QUALITY_NAMES.get(s.quality, "good")

                if s.value_type == int(ValueType.TEXT):
                    # A state word, not a number. Falling through to the gauge
                    # branch stored float(double_value) - which is 0 for every
                    # text sample - so a UPS running on battery was recorded as
                    # the number zero in a table of measurements.
                    text_rows.append(writer.TextRow(
                        ts=observed, device_id=s.device_id, metric_id=metric_id,
                        instance=s.instance, value=s.text_value, quality=quality))
                    self._note_hot(hot, ws_frames, s.device_id, s.metric,
                                   s.text_value, observed, quality)
                    continue

                if s.value_type == int(ValueType.BOOL):
                    bool_rows.append(writer.BoolRow(
                        ts=observed, device_id=s.device_id, metric_id=metric_id,
                        instance=s.instance, value=bool(s.bool_value), quality=quality))
                    self._note_hot(hot, ws_frames, s.device_id, s.metric,
                                   bool(s.bool_value), observed, quality)
                    # Booleans reach the rules too. Equipment publishes its own
                    # faults as binary points - a BACnet Alarm_Leak, a Modbus
                    # breaker bit - and until now they were stored and never
                    # evaluated: 38 points streaming in from the plant that
                    # could not raise anything. Carried as 1.0/0.0 so one dwell
                    # and one clear path serve both kinds of rule.
                    rule_inputs.append({
                        "device_id": s.device_id, "device_type": ctx.device_type,
                        "metric": s.metric, "instance": s.instance,
                        "value": 1.0 if s.bool_value else 0.0,
                        "observed_at": observed, "endpoint_id": s.endpoint_id,
                    })
                    continue

                if s.value_type == int(ValueType.COUNTER):
                    key = rates.baseline_key(s.endpoint_id, s.metric, s.instance)
                    prev = baselines.get(key)
                    rate, reason = rates.derive(
                        metric=s.metric, instance=s.instance, current=s.uint_value,
                        observed_at_us=s.observed_at or s.collected_at,
                        counter_bits=s.counter_bits or 64,
                        counter_reset=bool(s.counter_reset), baseline=prev,
                        max_gap_s=self.settings.ingest_max_counter_gap_s)
                    # The baseline advances whether or not a rate was emitted.
                    baseline_writes[key] = \
                        f"{s.uint_value}:{s.observed_at or s.collected_at}"
                    sample_rows.append(writer.SampleRow(
                        ts=observed, device_id=s.device_id, metric_id=metric_id,
                        instance=s.instance, value=float(s.uint_value), quality=quality))
                    if rate is not None:
                        rate_metric_id = self.cache.metric_id(rate.metric)
                        if rate_metric_id is not None:
                            sample_rows.append(writer.SampleRow(
                                ts=observed, device_id=s.device_id,
                                metric_id=rate_metric_id, instance=s.instance,
                                value=rate.value, quality=quality))
                            self._note_hot(hot, ws_frames, s.device_id, rate.metric,
                                           rate.value, observed, quality)
                    elif reason not in (rates.DiscardReason.NO_BASELINE,
                                        rates.DiscardReason.NO_RATE_TARGET):
                        log.debug("rate discarded", metric=s.metric, reason=reason,
                                  device_id=s.device_id)
                    continue

                # gauge / delta
                value = float(s.double_value)
                if not rates.in_valid_range(s.metric, value):
                    quality = "suspect"
                sample_rows.append(writer.SampleRow(
                    ts=observed, device_id=s.device_id, metric_id=metric_id,
                    instance=s.instance, value=value, quality=quality))
                self._note_hot(hot, ws_frames, s.device_id, s.metric, value,
                               observed, quality)
                rule_inputs.append({
                    "device_id": s.device_id, "device_type": ctx.device_type,
                    "metric": s.metric, "instance": s.instance, "value": value,
                    "observed_at": observed, "endpoint_id": s.endpoint_id,
                })
                if s.endpoint_id:
                    prev = produced.get(s.endpoint_id)
                    if prev is None or observed > prev:
                        produced[s.endpoint_id] = observed

            await writer.copy_samples(session, sample_rows)
            await writer.insert_bools(session, bool_rows)
            await writer.insert_texts(session, text_rows)
            await writer.upsert_device_state(session, list(hot.values()))
            # Which endpoints actually produced something, and when. Staleness
            # detection reads this against the endpoint's poll success to tell
            # "reachable and reporting" from "reachable and silent".
            await writer.touch_endpoint_telemetry(session, produced)
            alarm_actions = await self.alarms.evaluate_samples(session, rule_inputs)

        # after commit
        await self._store_baselines(baseline_writes)
        await self.fanout.telemetry(ws_frames)
        for action in alarm_actions:
            await self.fanout.alarm(action.kind, action.alarm)

        if unknown_metrics:
            # The collector validates against the registry at emit time, so this
            # means the two are running different contract versions.
            log.warning("dropped samples with unknown metrics",
                        metrics=sorted(unknown_metrics)[:10],
                        count=len(unknown_metrics))
        if unresolved_interfaces:
            # Kept, not dropped - but worth saying. A port inventory does not
            # know about is either newly patched, or a name one plane reports
            # in a form nothing here recognises, and the second case is how a
            # single port silently becomes two series.
            log.warning("interface instances not found in inventory",
                        examples=sorted(unresolved_interfaces)[:10],
                        count=len(unresolved_interfaces))
        # Batch-level counts at INFO: one line per consumed batch is cheap, and
        # without it "the numbers are not moving" is unanswerable.
        log.info("telemetry ingested", received=len(samples),
                 numeric=len(sample_rows), bools=len(bool_rows),
                 texts=len(text_rows), devices=len(hot),
                 unresolved_interfaces=len(unresolved_interfaces),
                 skipped=len(samples) - len(sample_rows) - len(bool_rows)
                 - len(text_rows))

    def _note_hot(self, hot: dict, frames: dict, device_id: str, metric: str,
                  value, observed: datetime, quality: str) -> None:
        if metric not in self.cache.hot_metrics:
            return
        entry = hot.get(device_id)
        if entry is None:
            entry = writer.HotUpdate(device_id=device_id, last_seen=observed, metrics={})
            hot[device_id] = entry
        entry.metrics[metric] = {"v": value, "t": observed.isoformat(), "q": quality}
        entry.last_seen = max(entry.last_seen, observed)
        column = _HOT_COLUMNS.get(metric)
        if column and isinstance(value, (int, float)):
            setattr(entry, column, float(value))
        frames.setdefault(device_id, {})[metric] = {
            "v": value, "u": METRICS[metric].unit, "q": quality}

    async def _handle_events(self, payloads: list[dict]) -> None:
        """Persist events, then drive the alarm lifecycle from them.

        The event row is written whatever happens, including for a trap whose
        source resolved to no device: dropping those is how an outage becomes
        "the DCIM never saw it".
        """
        events = []
        for raw in payloads:
            batch = EventBatch.from_dict(raw)
            events.extend(batch.events)
        if not events:
            return

        fresh = [e for e in events if await self._claim_event(e.dedup_key)]
        if not fresh:
            log.debug("all events in batch were duplicates", count=len(events))
            return

        rows, actions = [], []
        async with unit_of_work() as session:
            await self.alarms.ensure_rules(session)
            for e in fresh:
                observed = ts_to_dt(e.observed_at) or datetime.now(UTC)
                clears = [c for c in (e.varbinds.get("_clears") or "").split(",") if c]
                payload = {
                    "device_id": e.device_id or None,
                    "endpoint_id": e.endpoint_id or None,
                    "source_ip": e.source_ip or None,
                    "event_type": e.event_type,
                    "instance": e.instance,
                    "severity": _SEVERITY_NAMES.get(e.severity, "INFO"),
                    "is_clear": bool(e.is_clear),
                    "clears": clears,
                    "message": e.message,
                    "observed_at": observed,
                    "source": "snmp_trap",
                    "varbinds": dict(e.varbinds),
                    "dedup_key": e.dedup_key,
                }
                rows.append(event_row(payload))
                action = await self.alarms.handle_event(session, payload)
                if action:
                    actions.append(action)
            await repo_alarms.insert_events(session, rows)

        for action in actions:
            await self.fanout.alarm(action.kind, action.alarm)
        for row in rows:
            await self.fanout.event({
                "event_type": row["event_type"], "severity": row["severity"],
                "message": row["message"], "device_id": row["device_id"],
                "source_ip": row["source_ip"],
                "ts": row["ts"].isoformat() if row["ts"] else None,
            })
        log.info("events ingested", received=len(events), fresh=len(fresh),
                 alarms=len(actions))

    async def _claim_event(self, dedup_key: str) -> bool:
        """True the first time a dedup key is seen.

        At-least-once delivery means a redelivered trap would otherwise bump an
        alarm's occurrence count and re-notify. Redis rather than a database
        constraint, because a hypertable's unique index must include the
        partition column and so cannot deduplicate across time.
        """
        if not dedup_key:
            return True
        return bool(await self.redis.set(f"dcim:ev:{dedup_key}", "1",
                                         nx=True, ex=86400))

    async def _handle_endpoint_state(self, payloads: list[dict]) -> None:
        comm_actions = []
        async with unit_of_work() as session:
            for raw in payloads:
                st = EndpointState.from_dict(raw)
                status = _COMM_STATUS.get(st.status, "UNKNOWN")
                await writer.apply_endpoint_state(session, {
                    "endpoint_id": st.endpoint_id,
                    "status": status,
                    "last_success": ts_to_dt(st.last_success),
                    "last_failure": ts_to_dt(st.last_failure),
                    "consecutive_failures": st.consecutive_failures,
                    "last_error": st.last_error or None,
                    "last_error_class": st.last_error_class or None,
                    "latency_ms": st.latency_ms or None,
                    "collector_id": st.collector_id or None,
                    # last_seen is the poll attempt, not the transition time.
                    # Older collectors do not send it, so fall back to
                    # changed_at rather than writing NULL over a good value.
                    "last_seen": ts_to_dt(st.last_seen or st.changed_at),
                    "poll_count": st.poll_count,
                    "fail_count": st.fail_count,
                    "timeout_count": st.timeout_count,
                    "auth_fail_count": st.auth_fail_count,
                    "is_refresh": st.is_refresh,
                })

                # A refresh means the status did NOT change. Broadcasting it
                # would push a websocket update per endpoint per minute to
                # every connected browser, and re-running the alarm engine on
                # an unchanged status is at best wasted work and at worst a
                # re-notification of an alarm the operator already saw.
                if st.is_refresh:
                    continue

                await self.fanout.device_status(st.device_id, status, None)

                ctx = self.cache.devices.get(st.device_id)
                ep = self.cache.endpoints.get(st.endpoint_id)
                if ctx is not None and ep is not None:
                    action = await self.alarms.handle_endpoint_state(
                        session, device_id=st.device_id,
                        endpoint_id=st.endpoint_id, status=status,
                        protocol=ep.protocol, device_name=ctx.name,
                        last_error=st.last_error or None)
                    if action:
                        comm_actions.append(action)

        for action in comm_actions:
            await self.fanout.alarm(action.kind, action.alarm)

    async def _handle_heartbeat(self, payloads: list[dict]) -> None:
        async with unit_of_work() as session:
            for raw in payloads:
                hb = CollectorHeartbeat.from_dict(raw)
                await session.execute(text("""
                    INSERT INTO collector_instance
                        (id, version, hostname, started_at, last_heartbeat,
                         endpoints_owned, endpoints_online, status, stats)
                    VALUES (:id, :version, :hostname, :started_at, now(),
                            :owned, :online, 'HEALTHY', CAST(:stats AS jsonb))
                    ON CONFLICT (id) DO UPDATE SET
                        version = EXCLUDED.version,
                        hostname = EXCLUDED.hostname,
                        started_at = EXCLUDED.started_at,
                        last_heartbeat = now(),
                        endpoints_owned = EXCLUDED.endpoints_owned,
                        endpoints_online = EXCLUDED.endpoints_online,
                        status = EXCLUDED.status,
                        stats = EXCLUDED.stats
                """), {
                    "id": hb.collector_id, "version": hb.version or None,
                    "hostname": hb.hostname or None,
                    "started_at": ts_to_dt(hb.started_at),
                    "owned": hb.endpoints_owned, "online": hb.endpoints_online,
                    "stats": json.dumps({
                        "polls_total": hb.polls_total, "polls_failed": hb.polls_failed,
                        "traps_received": hb.traps_received,
                        "queue_depth": hb.queue_depth,
                        "active_streams": hb.active_streams,
                        "assignment_version": hb.assignment_version,
                    }),
                })

    # ------------------------------------------------------------- baselines

    async def _load_baselines(self, keys: list[str]) -> dict[str, rates.Baseline]:
        if not keys:
            return {}
        values = await self.redis.mget([f"dcim:ctr:{k}" for k in keys])
        out: dict[str, rates.Baseline] = {}
        for key, raw in zip(keys, values, strict=True):
            if not raw:
                continue
            try:
                value, ts = raw.decode().split(":", 1)
                out[key] = rates.Baseline(value=int(value), observed_at_us=int(ts))
            except (ValueError, AttributeError):
                continue
        return out

    async def _store_baselines(self, writes: dict[str, str]) -> None:
        if not writes:
            return
        pipe = self.redis.pipeline()
        for key, value in writes.items():
            pipe.set(f"dcim:ctr:{key}", value, ex=3600)
        await pipe.execute()


async def _amain() -> None:
    configure_logging(service="dcim-ingest")
    worker = IngestWorker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows lacks add_signal_handler
            loop.add_signal_handler(sig, worker.stop)
    await worker.run()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain())


if __name__ == "__main__":
    main()
