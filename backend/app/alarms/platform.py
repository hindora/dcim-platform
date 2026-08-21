"""Alarms about the monitoring system itself.

Split deliberately into a pure evaluator and an I/O layer elsewhere: what makes
a platform alarm correct is the thresholds and the states, and those should be
testable without a database, a Redis or a clock.

The design point that matters more than any threshold here: **the evaluator
must not be the only thing that can report its own death.** It runs in the
ingest worker, because that is the only process that knows how long a sample
took to travel. If the worker dies, nothing in the worker will say so. The
worker therefore writes a heartbeat, and the API checks that heartbeat's age
independently - ``ingest_worker_stale`` is raised by a process that is not the
one being watched.

The second point is about silence. Every check here treats "no reading" as its
own state rather than as a zero. A collector table with no rows is not a fleet
of healthy collectors; a lag gauge with no value is not a lag of zero. Where the
answer is unknown the finding says unknown, and where the unknown is itself
alarming it raises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Pipeline latency: publish -> committed row. Sub-second when healthy, so these
# thresholds have room. This is NOT data freshness - see core/metrics.py.
INGEST_LAG_WARNING_S = 60.0
INGEST_LAG_CRITICAL_S = 300.0

# A collector heartbeats every 30 s by contract; 60 s is two missed beats.
COLLECTOR_STALE_S = 60.0

# The worker's own heartbeat, checked by the API rather than by the worker.
WORKER_STALE_S = 120.0

# An assignment older than this means the collector is polling a plan that may
# no longer match inventory.
ASSIGNMENT_STALE_S = 300.0

# Queue depth past this share of capacity is a collector that is losing.
PUBLISH_QUEUE_WARN_FRACTION = 0.8

# Sustained pool saturation. A momentary spike is normal under load; thirty
# seconds of it means requests are queueing on connections.
DB_POOL_SATURATED_S = 30.0

WARNING = "WARNING"
MAJOR = "MAJOR"
CRITICAL = "CRITICAL"

SOURCE = "platform"

# Every type this module can raise. The API uses it to separate platform alarms
# from device alarms without string-matching on prefixes.
PLATFORM_ALARM_TYPES = (
    "ingest_lag_high",
    "ingest_stalled",
    "ingest_worker_stale",
    "collector_stale",
    "collector_degraded",
    "assignment_stale",
    "db_pool_exhausted",
)


@dataclass
class Finding:
    """One platform alarm that should be open, with the reason in words."""

    alarm_type: str
    instance: str
    severity: str
    message: str
    value: float | None = None
    threshold: float | None = None


@dataclass
class Collector:
    collector_id: str
    heartbeat_age_s: float | None
    status: str | None = None
    endpoints_owned: int = 0
    endpoints_online: int = 0
    assignment_age_s: float | None = None
    publish_queue_depth: int | None = None
    publish_queue_capacity: int | None = None
    publish_dropped: int = 0


@dataclass
class Signals:
    """Everything the evaluator is allowed to look at.

    Gathered by the caller so that the rules can be tested against any state,
    including the states that are hard to produce on purpose.
    """

    ingest_lag_s: float | None = None
    telemetry_age_s: float | None = None
    telemetry_present: bool = True
    worker_heartbeat_age_s: float | None = None
    poll_interval_s: float = 120.0
    collectors: list[Collector] = field(default_factory=list)
    collectors_expected: int = 0
    db_pool_saturated_for_s: float = 0.0
    stream_pending: dict[str, int] = field(default_factory=dict)


def _lag_severity(lag: float) -> str | None:
    if lag >= INGEST_LAG_CRITICAL_S:
        return CRITICAL
    if lag >= INGEST_LAG_WARNING_S:
        return WARNING
    return None


def evaluate(signals: Signals) -> list[Finding]:
    """Which platform alarms should be open right now."""
    out: list[Finding] = []

    # --- the pipeline ---------------------------------------------------------
    lag = signals.ingest_lag_s
    if lag is not None:
        severity = _lag_severity(lag)
        if severity:
            out.append(Finding(
                alarm_type="ingest_lag_high", instance="telemetry.v1",
                severity=severity, value=round(lag, 1),
                threshold=(INGEST_LAG_CRITICAL_S if severity == CRITICAL
                           else INGEST_LAG_WARNING_S),
                message=(
                    f"Telemetry is taking {lag:.0f}s to travel from the "
                    f"collector to the database. Samples are being written, "
                    f"but everything read from this platform - dashboards, "
                    f"alarms, analytics - is that far behind the datacenter")))

    # Freshness is judged against the poll interval, not against zero. A fleet
    # polled every 120 s is a fleet whose newest sample is routinely 120 s old,
    # and alerting at 60 s on that would fire permanently and be turned off,
    # which is the worst outcome an alert can have.
    if not signals.telemetry_present:
        out.append(Finding(
            alarm_type="ingest_stalled", instance="telemetry.v1",
            severity=CRITICAL,
            message=("No telemetry has ever been written. Either the platform "
                     "has never collected, or the table has been truncated - "
                     "either way nothing on this platform is measuring "
                     "anything")))
    elif signals.telemetry_age_s is not None:
        # Three missed poll cycles. Two is a hiccup; three is a pattern.
        limit = max(3 * signals.poll_interval_s, INGEST_LAG_CRITICAL_S)
        if signals.telemetry_age_s >= limit:
            out.append(Finding(
                alarm_type="ingest_stalled", instance="telemetry.v1",
                severity=CRITICAL, value=round(signals.telemetry_age_s, 1),
                threshold=limit,
                message=(
                    f"The newest telemetry sample is "
                    f"{signals.telemetry_age_s / 60:.0f} minutes old against a "
                    f"{signals.poll_interval_s:.0f}s poll interval. The absence "
                    f"of device alarms right now means nothing is being "
                    f"measured, not that the datacenter is well")))

    # Raised by the API, about the worker, precisely because a dead worker
    # cannot raise it about itself.
    #
    # No heartbeat at all is the same finding as an old one, and it took a live
    # run to notice: against a worker too old to write heartbeats the age came
    # back None, the `is not None` guard skipped the check entirely, and the
    # platform reported it was "monitoring itself and finding nothing wrong"
    # while it could not see the worker at all. Absence is the more serious
    # case, not the exempt one - the worker writes a heartbeat every tick, so
    # the only ways to have none are: never started, died before the first
    # tick, or too old a build to report one.
    # Freshness is EVIDENCE of a live worker, and it changes what a missing
    # heartbeat means. Telemetry arriving means something is draining the
    # stream whatever its build reports, so the finding is that the platform
    # cannot see its worker - a monitoring gap - not that ingestion has
    # stopped. Treating those as the same critical fault made /ready fail
    # during a version skew where the pipeline was demonstrably healthy, which
    # in a real deployment pulls a working API out of the load balancer.
    heartbeat_missing = (signals.worker_heartbeat_age_s is None
                         or signals.worker_heartbeat_age_s >= WORKER_STALE_S)
    telemetry_flowing = (
        signals.telemetry_present
        and signals.telemetry_age_s is not None
        and signals.telemetry_age_s < 3 * signals.poll_interval_s)

    if heartbeat_missing:
        age = signals.worker_heartbeat_age_s
        when = "has never checked in" if age is None else f"last checked in {age:.0f}s ago"
        if telemetry_flowing:
            out.append(Finding(
                alarm_type="ingest_worker_stale", instance="ingest",
                severity=WARNING, value=age, threshold=WORKER_STALE_S,
                message=(
                    f"The ingest worker {when}, but telemetry is still "
                    f"arriving, so something is draining the stream. This is a "
                    f"blind spot in the monitoring - an older worker build, or "
                    f"a heartbeat that cannot be written - rather than an "
                    f"ingestion outage")))
        else:
            out.append(Finding(
                alarm_type="ingest_worker_stale", instance="ingest",
                severity=CRITICAL, value=age, threshold=WORKER_STALE_S,
                message=(
                    f"The ingest worker {when} and no telemetry is arriving. "
                    f"Nothing is draining the stream, and the worker cannot "
                    f"report this about itself")))

    # --- collectors -----------------------------------------------------------
    if signals.collectors_expected and not signals.collectors:
        out.append(Finding(
            alarm_type="collector_stale", instance="*", severity=CRITICAL,
            message=(f"No collector has ever checked in, against "
                     f"{signals.collectors_expected} expected. Nothing is "
                     f"polling the fleet")))

    for c in signals.collectors:
        if c.heartbeat_age_s is None or c.heartbeat_age_s >= COLLECTOR_STALE_S:
            age = ("never" if c.heartbeat_age_s is None
                   else f"{c.heartbeat_age_s:.0f}s ago")
            out.append(Finding(
                alarm_type="collector_stale", instance=c.collector_id,
                severity=CRITICAL, value=c.heartbeat_age_s,
                threshold=COLLECTOR_STALE_S,
                message=(
                    f"Collector {c.collector_id} last checked in {age}. The "
                    f"{c.endpoints_owned} endpoints it owns are not being "
                    f"polled by anything")))
            # A collector that is not talking to us cannot also be judged
            # degraded or stale-assignment; those would be three alarms for one
            # fault, and the operator has to read all three to find the one
            # that matters.
            continue

        # Reported by the collector, and the two counts disagreed live: 1386
        # online against 1340 owned. One of them is measuring something other
        # than what its name says, and a page that quietly prints 103% teaches
        # an operator to stop reading the number.
        if c.endpoints_owned and c.endpoints_online > c.endpoints_owned:
            out.append(Finding(
                alarm_type="collector_degraded", instance=c.collector_id,
                severity=WARNING, value=float(c.endpoints_online),
                threshold=float(c.endpoints_owned),
                message=(
                    f"Collector {c.collector_id} reports {c.endpoints_online} "
                    f"endpoints online out of {c.endpoints_owned} owned. The "
                    f"counts disagree, so neither can be relied on for "
                    f"coverage")))
        elif c.publish_dropped > 0:
            out.append(Finding(
                alarm_type="collector_degraded", instance=c.collector_id,
                severity=MAJOR, value=float(c.publish_dropped),
                message=(
                    f"Collector {c.collector_id} has dropped "
                    f"{c.publish_dropped} publish batches. Those samples do "
                    f"not exist anywhere - this is data loss, not delay")))
        elif (c.publish_queue_depth is not None and c.publish_queue_capacity):
            fraction = c.publish_queue_depth / c.publish_queue_capacity
            if fraction >= PUBLISH_QUEUE_WARN_FRACTION:
                out.append(Finding(
                    alarm_type="collector_degraded", instance=c.collector_id,
                    severity=WARNING, value=round(fraction * 100, 1),
                    threshold=PUBLISH_QUEUE_WARN_FRACTION * 100,
                    message=(
                        f"Collector {c.collector_id} publish queue is "
                        f"{fraction * 100:.0f}% full. It is producing faster "
                        f"than it can publish, and will start dropping")))

        if c.assignment_age_s is not None and c.assignment_age_s >= ASSIGNMENT_STALE_S:
            out.append(Finding(
                alarm_type="assignment_stale", instance=c.collector_id,
                severity=WARNING, value=round(c.assignment_age_s, 1),
                threshold=ASSIGNMENT_STALE_S,
                message=(
                    f"Collector {c.collector_id} is working from an assignment "
                    f"{c.assignment_age_s / 60:.0f} minutes old. Devices added "
                    f"or retired since then are not reflected in what it polls")))

    # --- database -------------------------------------------------------------
    if signals.db_pool_saturated_for_s >= DB_POOL_SATURATED_S:
        out.append(Finding(
            alarm_type="db_pool_exhausted", instance="default",
            severity=MAJOR, value=round(signals.db_pool_saturated_for_s, 1),
            threshold=DB_POOL_SATURATED_S,
            message=(
                f"Every database connection has been in use for "
                f"{signals.db_pool_saturated_for_s:.0f}s. Requests are queueing "
                f"for a connection before they even reach a query")))

    return out


def diff(current: list[Finding], open_keys: set[tuple[str, str]]
         ) -> tuple[list[Finding], list[tuple[str, str]]]:
    """What to raise and what to clear.

    Clearing is driven by absence from the current findings rather than by an
    explicit recovery signal, because most of these conditions have no recovery
    event - a collector that starts heartbeating again does not announce it.
    """
    current_keys = {(f.alarm_type, f.instance) for f in current}
    to_clear = sorted(open_keys - current_keys)
    return current, to_clear


def summarise(findings: list[Finding]) -> dict[str, Any]:
    """A one-line health verdict for the collector page and the dashboard."""
    if not findings:
        return {"healthy": True, "severity": None,
                "summary": "the platform is monitoring itself and finding nothing wrong"}
    order = {CRITICAL: 3, MAJOR: 2, WARNING: 1}
    worst = max(findings, key=lambda f: order.get(f.severity, 0))
    return {
        "healthy": False,
        "severity": worst.severity,
        "summary": worst.message,
        "count": len(findings),
    }
