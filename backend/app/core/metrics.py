"""Prometheus metrics for the backend and the ingest worker.

The collector has had its metrics since commit one; this is the other half.

Two things are deliberate.

**Cardinality discipline.** No metric is ever labelled with a device id, an
endpoint id or a raw URL path. Per-device detail belongs in the ``poll_result``
hypertable, which is built for it. API paths are templated (``/devices/{id}``,
not ``/devices/2f9c...``) or the label set grows with the fleet and takes the
Prometheus install with it.

**Lag is measured twice, because it is two questions.**

``dcim_ingest_lag_seconds`` is PIPELINE latency: how long a sample took to get
from the collector's publish to a committed row. In health it is well under a
second, and the spec's 60 s warning threshold is meaningful against it.

``dcim_telemetry_age_seconds`` is DATA freshness: how old the newest sample is.
In perfect health it sits anywhere up to the poll interval - 120 s for power on
this fleet - so alerting on it at 60 s would fire permanently. It is the number
that matters when the collector stops, and the two are not interchangeable.

The freshness gauge is also the one that must not go quiet during an outage.
Computed as ``max(ts)`` over a bounded lookback, it returns NULL once the outage
is longer than the window, and a metric that disappears exactly when the
pipeline dies is worse than no metric: every dashboard reads it as "no data" and
every threshold alert stops evaluating. It is computed unbounded here so it
grows without limit, and a separate gauge reports whether any sample has ever
been seen at all.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# A dedicated registry rather than the global default. The default registry is
# process-wide and pre-populated, and re-registering a name on it raises - which
# makes anything that constructs metrics untestable and makes a second import
# fatal.
REGISTRY = CollectorRegistry()

# Latency buckets sized for what is being measured. The API's default
# prometheus buckets top out at 10 s, which is fine; the ingest path needs
# resolution well below a second, because a healthy pipeline lives there.
_API_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_LAG_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0)

# --- API ----------------------------------------------------------------------

api_requests = Counter(
    "dcim_api_requests_total", "API requests by method, templated path and status",
    ["method", "path", "status"], registry=REGISTRY)

api_duration = Histogram(
    "dcim_api_request_duration_seconds", "API request duration",
    ["method", "path"], buckets=_API_BUCKETS, registry=REGISTRY)

# --- database -----------------------------------------------------------------

db_pool = Gauge(
    "dcim_db_pool_connections", "Database pool connections by state",
    ["state"], registry=REGISTRY)

# --- websocket ----------------------------------------------------------------

ws_connections = Gauge(
    "dcim_ws_connections", "Open websocket connections", registry=REGISTRY)

ws_frames = Counter(
    "dcim_ws_frames_sent_total", "Websocket frames sent by event type",
    ["event"], registry=REGISTRY)

ws_slow_disconnects = Counter(
    "dcim_ws_slow_consumer_disconnects_total",
    "Websocket clients dropped for not keeping up", registry=REGISTRY)

# --- ingest -------------------------------------------------------------------

ingest_messages = Counter(
    "dcim_ingest_messages_total", "Stream messages consumed by result",
    ["stream", "result"], registry=REGISTRY)

# The single most important number in the system: publish -> committed row.
ingest_lag = Gauge(
    "dcim_ingest_lag_seconds",
    "Pipeline latency: seconds from collector publish to committed row",
    ["stream"], registry=REGISTRY)

ingest_lag_hist = Histogram(
    "dcim_ingest_lag_distribution_seconds",
    "Distribution of pipeline latency, so a p99 exists and not just a last value",
    ["stream"], buckets=_LAG_BUCKETS, registry=REGISTRY)

ingest_batch_size = Histogram(
    "dcim_ingest_batch_size", "Samples per consumed batch",
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500), registry=REGISTRY)

ingest_write_duration = Histogram(
    "dcim_ingest_write_duration_seconds", "Time to write one table",
    ["table"], buckets=_API_BUCKETS, registry=REGISTRY)

ingest_stream_pending = Gauge(
    "dcim_ingest_stream_pending", "Entries delivered but not acknowledged",
    ["stream"], registry=REGISTRY)

# Freshness, unbounded so that an outage makes it grow rather than vanish.
telemetry_age = Gauge(
    "dcim_telemetry_age_seconds",
    "Age of the newest telemetry sample. Healthy value is bounded by the poll "
    "interval, not by zero",
    registry=REGISTRY)

telemetry_seen = Gauge(
    "dcim_telemetry_samples_present",
    "1 if any telemetry has ever been written, 0 if the table is empty. "
    "Distinguishes an empty database from a stalled pipeline, which an age "
    "gauge alone cannot",
    registry=REGISTRY)

# --- alarms -------------------------------------------------------------------

alarms_active = Gauge(
    "dcim_alarms_active", "Active alarms by severity and origin",
    ["severity", "origin"], registry=REGISTRY)

alarm_transitions = Counter(
    "dcim_alarm_transitions_total", "Alarm transitions",
    ["action"], registry=REGISTRY)

alarm_eval_duration = Histogram(
    "dcim_alarm_eval_duration_seconds", "Alarm evaluation duration",
    buckets=_API_BUCKETS, registry=REGISTRY)

# --- collectors ---------------------------------------------------------------

collectors_up = Gauge(
    "dcim_collectors_up", "Collectors that have heartbeated inside the window",
    registry=REGISTRY)

collector_heartbeat_age = Gauge(
    "dcim_collector_heartbeat_age_seconds", "Age of the newest collector heartbeat",
    ["collector_id"], registry=REGISTRY)


@contextmanager
def observe(histogram: Histogram, **labels: str) -> Iterator[None]:
    """Time a block into a histogram, including when it raises.

    A write that fails after four seconds is exactly the write worth timing, so
    the duration is recorded on the way out either way.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        (histogram.labels(**labels) if labels else histogram).observe(elapsed)


def render() -> bytes:
    return generate_latest(REGISTRY)
