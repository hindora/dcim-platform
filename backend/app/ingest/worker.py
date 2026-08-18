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
from datetime import UTC, datetime

import msgpack
from redis.asyncio import Redis
from sqlalchemy import text

from app.contracts.messages_gen import (
    CollectorHeartbeat,
    EndpointState,
    Quality,
    Stream,
    TelemetryBatch,
    ValueType,
    ts_to_dt,
)
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.metrics_gen import METRICS
from app.db.session import dispose_engine, unit_of_work
from app.ingest import rates, writer
from app.ingest.enrich import InventoryCache
from app.ingest.fanout import Fanout

log = get_logger("ingest")

_QUALITY_NAMES = {int(q): q.name.lower() for q in Quality}
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
        self.redis: Redis = Redis.from_url(self.settings.redis_url)
        self.cache = InventoryCache()
        self.fanout = Fanout(self.redis)
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
        for stream in (Stream.TELEMETRY, Stream.ENDPOINTSTATE, Stream.HEARTBEAT):
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

        await self._reclaim_stale()

        entries = await self.redis.xreadgroup(
            self.settings.ingest_group, self.consumer,
            {Stream.TELEMETRY: ">", Stream.ENDPOINTSTATE: ">", Stream.HEARTBEAT: ">"},
            count=self.settings.ingest_batch_size,
            block=self.settings.ingest_block_ms,
        )
        if not entries:
            return

        for stream_name, messages in entries:
            name = stream_name.decode() if isinstance(stream_name, bytes) else stream_name
            ids = [mid for mid, _ in messages]
            payloads = [msgpack.unpackb(fields[b"p"], raw=False) for _, fields in messages]

            if name == Stream.TELEMETRY:
                await self._handle_telemetry(payloads)
            elif name == Stream.ENDPOINTSTATE:
                await self._handle_endpoint_state(payloads)
            elif name == Stream.HEARTBEAT:
                await self._handle_heartbeat(payloads)

            await self.redis.xack(name, self.settings.ingest_group, *ids)

    async def _reclaim_stale(self) -> None:
        """Take over entries a dead worker never acked."""
        for stream in (Stream.TELEMETRY, Stream.ENDPOINTSTATE, Stream.HEARTBEAT):
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
        hot: dict[str, writer.HotUpdate] = {}
        ws_frames: dict[str, dict] = {}
        unknown_metrics: set[str] = set()

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

                observed = ts_to_dt(s.observed_at) or ts_to_dt(s.collected_at) \
                    or datetime.now(UTC)
                quality = _QUALITY_NAMES.get(s.quality, "good")

                if s.value_type == int(ValueType.BOOL):
                    bool_rows.append(writer.BoolRow(
                        ts=observed, device_id=s.device_id, metric_id=metric_id,
                        instance=s.instance, value=bool(s.bool_value), quality=quality))
                    self._note_hot(hot, ws_frames, s.device_id, s.metric,
                                   bool(s.bool_value), observed, quality)
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

            await writer.copy_samples(session, sample_rows)
            await writer.insert_bools(session, bool_rows)
            await writer.upsert_device_state(session, list(hot.values()))

        # after commit
        await self._store_baselines(baseline_writes)
        await self.fanout.telemetry(ws_frames)

        if unknown_metrics:
            # The collector validates against the registry at emit time, so this
            # means the two are running different contract versions.
            log.warning("dropped samples with unknown metrics",
                        metrics=sorted(unknown_metrics)[:10],
                        count=len(unknown_metrics))
        log.debug("telemetry ingested", samples=len(sample_rows),
                  bools=len(bool_rows), devices=len(hot))

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

    async def _handle_endpoint_state(self, payloads: list[dict]) -> None:
        async with unit_of_work() as session:
            for raw in payloads:
                st = EndpointState.from_dict(raw)
                await writer.apply_endpoint_state(session, {
                    "endpoint_id": st.endpoint_id,
                    "status": _COMM_STATUS.get(st.status, "UNKNOWN"),
                    "last_success": ts_to_dt(st.last_success),
                    "last_failure": ts_to_dt(st.last_failure),
                    "consecutive_failures": st.consecutive_failures,
                    "last_error": st.last_error or None,
                    "last_error_class": st.last_error_class or None,
                    "latency_ms": st.latency_ms or None,
                    "collector_id": st.collector_id or None,
                    "last_seen": ts_to_dt(st.changed_at),
                })
                await self.fanout.device_status(
                    st.device_id, _COMM_STATUS.get(st.status, "UNKNOWN"), None)

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
