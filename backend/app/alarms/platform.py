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

from app.services import version_skew

# Pipeline latency: publish -> committed row. Sub-second when healthy, so these
# thresholds have room. This is NOT data freshness - see core/metrics.py.
INGEST_LAG_WARNING_S = 60.0
INGEST_LAG_CRITICAL_S = 300.0
# How long the lag has to STAY over the line before it is worth telling anyone.
#
# The pipeline is bursty by nature: a poll wave lands, the worker chews through
# it, and the transport delay spikes and drains. Measured here, three times in
# one hour - 89.8 s, 90.8 s, 106.8 s - each one raising a warning and clearing
# it within two minutes. Nothing was wrong, and because a platform condition
# paints a banner across every page, the estate looked degraded for most of an
# hour on account of a queue that was doing its job.
#
# Two minutes is longer than any burst observed draining, and far shorter than
# a genuine stall: the worst real one in this table sat at 59763 s. An
# indication that fires on every poll wave is one that gets ignored, which is
# the failure mode worth designing against.
INGEST_LAG_DWELL_S = 120.0

# A collector heartbeats every 30 s by contract; 60 s is two missed beats.
COLLECTOR_STALE_S = 60.0

# The worker's own heartbeat, checked by the API rather than by the worker.
WORKER_STALE_S = 120.0

# An assignment older than this means the collector is polling a plan that may
# no longer match inventory.
ASSIGNMENT_STALE_S = 300.0

# Queue depth past this share of capacity is a collector that is losing.
PUBLISH_QUEUE_WARN_FRACTION = 0.8

# How long after the drop counter last rose the collector stays degraded. Long
# enough to be seen on a console that is not being watched continuously, short
# enough that a recovered collector stops saying it is losing data.
PUBLISH_DROP_WINDOW_S = 900.0

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
    "mapping_mismatch",
    "db_pool_exhausted",
    "integration_credential_expiring",
    "integration_degraded",
    "integration_webhook_expiring",
    "pool_below_min_members",
    "collector_outdated",
    "collector_capacity_high",
    "pool_rate_budget_exceeded",
    "collector_misconfigured",
    "collector_duplicate",
)

#: A second process seen under one collector identity within this long is
#: still running: duplicates alternate heartbeats, so a live pair re-marks
#: it every twenty seconds or so.
DUPLICATE_WINDOW_S = 300.0

#: docs/26 Phase 5. SolarWinds warns at 85% of a poller's maximum polling
#: rate and starts stretching intervals at 100%; Zabbix's stock template
#: alerts on poller busy above 75%, Niagara keeps its poll scheduler under
#: 75%. 85 is the plan's line, drawn on MEASURED worker saturation - the
#: collector's own worker-time over a trailing five minutes - rather than on
#: a weighted estimate of what the load ought to cost.
CAPACITY_WARN_PCT = 85.0
#: Past this a collector has no headroom for a slow device or a retry storm.
CAPACITY_MAJOR_PCT = 95.0
#: A report over less than this is a process that just started: one slow
#: walk in its first ten seconds is not a capacity finding.
CAPACITY_MIN_WINDOW_S = 60.0

#: A pool's rate budget is the load the target network agreed to take - the
#: BMS supervisor's or serial gateway's ceiling, not the collector's. Each
#: collector enforces its share of it by deferring polls (collector
#: internal/throttle), so from the warning fraction up the budget, not the
#: poll profile, is what decides how fresh the pool's data is; past 100%
#: some collector is not enforcing it at all.
BUDGET_WARN_FRACTION = 0.85
#: Not 1.0: a pool held to its budget averages up to about 2% over it in
#: the 5-minute window, because a quiet spell may bank 5 s of rate as a
#: burst. "Not enforcing" must not fire on a pool that is.
BUDGET_OVER_FRACTION = 1.05

#: An Atlassian Cloud API token expires within a year of being minted, and the
#: whole pre-December-2024 generation was force-expired in spring 2026. A
#: lapsed credential means this platform stops opening tickets and NOTHING
#: says so - the integration still reads "enabled", the outbox still fills,
#: and the first anyone knows is a fault nobody was told about. So it is
#: treated as what it is: a visibility failure, on the same console as every
#: other one.
CREDENTIAL_WARNING_DAYS = 30
CREDENTIAL_MAJOR_DAYS = 7

#: Dead letters that mean the integration is not working, as opposed to one
#: malformed row. One is a mapping mistake somebody can fix; five in the queue
#: is a project key that no longer exists or a credential that was revoked.
DEAD_LETTER_MAJOR = 5

#: A Jira Cloud webhook registered through the REST API expires 30 days after
#: it is created, and the dispatcher refreshes it a week ahead. This is the
#: line for when that refresh has been failing: nothing errors when a
#: registration lapses, no bounce and no log line - the tickets simply stop
#: answering back, and the alarm an engineer closed stays ACTIVE forever.
WEBHOOK_WARNING_DAYS = 5
WEBHOOK_MAJOR_DAYS = 2


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
    #: sha256 of the mapping bundle this process is actually running. Empty
    #: for a collector built before the field existed, which is silence, not
    #: a mismatch - see mapping_mismatch below.
    mapping_bundle_sha: str = ""
    #: docs/26 Phase 7. Empty, or "dev" (main.go's default for a build that
    #: never went through a real release), is UNKNOWN to version_skew.classify
    #: - neither current nor ancient, just outside what skew can say anything
    #: about.
    version: str = ""
    #: docs/26 Phase 5: the collector's own capacity report, trailing
    #: window. None for a collector too old to send one, or one with a
    #: single reading so far - silence, never a zero load.
    capacity_busy_pct: float | None = None
    capacity_window_s: float = 0.0
    capacity_shed: int = 0
    capacity_late: int = 0
    #: What the collector could not apply of its own configuration - a trap
    #: or health port already in use, most often. Empty when clean.
    config_error: str = ""
    #: Seconds since a heartbeat proved a second live process under this
    #: identity; None when never.
    duplicate_age_s: float | None = None


@dataclass
class Pool:
    """One collector_pool, as the monitor sees it - docs/26 Phase 6.

    healthy_accepting_members counts collectors placed in this pool
    (pool_id matches) whose heartbeat is under COLLECTOR_STALE_S and whose
    state is 'active' - the same two facts services.sharding.Collector
    calls healthy/accepting, gathered here independently rather than
    reused, since this module is intentionally a pure function of
    whatever numbers its caller hands it.
    """

    pool_id: str
    name: str
    min_members: int
    healthy_accepting_members: int
    #: docs/26 Phase 5. The pool's configured ceiling, and what live
    #: collectors measured publishing from its endpoints - summed over every
    #: collector reporting points for it, member or not, since a pinned
    #: endpoint can be polled from outside its pool. None when unset, or
    #: when no collector reports per-pool points yet.
    rate_budget_points_per_s: float | None = None
    points_per_s: float | None = None


@dataclass
class Integration:
    """One configured outbound integration, as the monitor sees it."""

    id: str
    name: str
    #: Days until the stored credential lapses. Negative means it already has,
    #: which is the case that actually happens and the one that sorts first.
    days_left: float | None = None
    dead_letters: int = 0
    #: Days until the INBOUND registration lapses. None for a hand-registered
    #: webhook, which never expires, and for an integration with no inbound
    #: half at all - both of which are "nothing to watch", not "overdue".
    webhook_days_left: float | None = None


@dataclass
class Signals:
    """Everything the evaluator is allowed to look at.

    Gathered by the caller so that the rules can be tested against any state,
    including the states that are hard to produce on purpose.
    """

    ingest_lag_s: float | None = None
    # How long ingest_lag_s has been continuously over the warning line.
    # Tracked by the caller so the rules stay a pure function of what they
    # are handed, and can still be tested against states that are hard to
    # produce on purpose.
    ingest_lag_sustained_s: float = 0.0
    telemetry_age_s: float | None = None
    telemetry_present: bool = True
    worker_heartbeat_age_s: float | None = None
    poll_interval_s: float = 120.0
    collectors: list[Collector] = field(default_factory=list)
    pools: list[Pool] = field(default_factory=list)
    #: This running platform's own release (app.__version__) - docs/26
    #: Phase 7's "N". Empty disables skew checking entirely (version_skew.
    #: classify returns UNKNOWN for an unparseable platform_version too),
    #: which is the honest state for a dev checkout with no real version.
    platform_version: str = ""
    collectors_expected: int = 0
    db_pool_saturated_for_s: float = 0.0
    stream_pending: dict[str, int] = field(default_factory=dict)
    integrations: list[Integration] = field(default_factory=list)
    #: sha256 of the platform's own contracts/mappings, computed fresh each
    #: gather() - see app/services/mapping_bundle.py. None means it could not
    #: be computed (the directory is missing), in which case the check is
    #: skipped rather than raised on: an alarm judged against its own broken
    #: input is worse than no alarm.
    expected_mapping_sha: str | None = None


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
        # A spike that drains is the pipeline working, not the pipeline
        # failing. Only a lag that persists is worth a banner.
        if severity and signals.ingest_lag_sustained_s < INGEST_LAG_DWELL_S:
            severity = None
        if severity:
            out.append(Finding(
                alarm_type="ingest_lag_high", instance="telemetry.v1",
                severity=severity, value=round(lag, 1),
                threshold=(INGEST_LAG_CRITICAL_S if severity == CRITICAL
                           else INGEST_LAG_WARNING_S),
                message=(
                    f"Telemetry has been taking {lag:.0f}s to travel from the "
                    f"collector to the database for "
                    f"{signals.ingest_lag_sustained_s / 60:.0f} min. Samples "
                    f"are being written, but everything read from this "
                    f"platform - dashboards, alarms, analytics - is that far "
                    f"behind the datacenter")))

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
        if c.heartbeat_age_s is None and not c.endpoints_owned:
            # Created ahead of its install and never run. It owns nothing yet
            # - the hash waits for a first heartbeat - so nothing is going
            # unpolled on its account; the finding is the missing machine.
            out.append(Finding(
                alarm_type="collector_stale", instance=c.collector_id,
                severity=WARNING, threshold=COLLECTOR_STALE_S,
                message=(
                    f"Collector {c.collector_id} has been created but has "
                    f"never checked in. It takes no work until it does")))
            continue
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
                    f"polled by anything, and show UNKNOWN until it returns")))
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
                    f"Collector {c.collector_id} has shed "
                    f"{c.publish_dropped} samples from its publish buffer. "
                    f"Those samples do not exist anywhere - this is data "
                    f"loss, not delay")))
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

        # Absence is silence, not a mismatch: a collector built before this
        # field existed sends nothing, and an unknown expected digest (the
        # platform's own contracts/mappings could not be read) means the
        # comparison itself is untrustworthy. Either way the honest answer is
        # to say nothing rather than raise on a question that cannot be
        # answered.
        if (c.mapping_bundle_sha and signals.expected_mapping_sha
                and c.mapping_bundle_sha != signals.expected_mapping_sha):
            out.append(Finding(
                alarm_type="mapping_mismatch", instance=c.collector_id,
                severity=WARNING,
                message=(
                    f"Collector {c.collector_id} is running mapping data "
                    f"{c.mapping_bundle_sha[:12]}, not the platform's "
                    f"{signals.expected_mapping_sha[:12]}. It may be polling "
                    f"metrics this release does not expect, or missing ones "
                    f"this release added")))

        # docs/26 Phase 7. Zabbix's "outdated keeps collecting" rule, not a
        # hard version lock - nothing here refuses this collector's data;
        # see version_skew's own docstring for why. UNKNOWN (unparseable on
        # either side - most commonly a "dev" build) raises nothing: it is
        # not this evaluator's place to guess at a policy neither version
        # string can support.
        skew = version_skew.classify(signals.platform_version, c.version)
        if skew == version_skew.OUTDATED:
            out.append(Finding(
                alarm_type="collector_outdated", instance=c.collector_id,
                severity=WARNING,
                message=(
                    f"Collector {c.collector_id} is running {c.version}, two "
                    f"releases behind {signals.platform_version}. It keeps "
                    f"collecting, but is not being given new endpoints until "
                    f"it upgrades")))
        elif skew == version_skew.REJECTED:
            out.append(Finding(
                alarm_type="collector_outdated", instance=c.collector_id,
                severity=MAJOR,
                message=(
                    f"Collector {c.collector_id} is running {c.version}, too "
                    f"far behind {signals.platform_version} to trust. It "
                    f"keeps collecting - this platform does not hard-block "
                    f"an old collector's data - but it needs upgrading soon")))

        # Two processes under one identity: both poll everything the
        # collector owns, so every counter-derived rate is wrong and every
        # endpoint is polled twice. MAJOR - it is data corruption, not risk.
        if c.duplicate_age_s is not None and c.duplicate_age_s < DUPLICATE_WINDOW_S:
            out.append(Finding(
                alarm_type="collector_duplicate", instance=c.collector_id,
                severity=MAJOR,
                message=(
                    f"Two processes are running as collector {c.collector_id}: "
                    f"its heartbeats come from two different start times. Both "
                    f"poll everything it owns, so its endpoints are polled twice "
                    f"and their rates are wrong. Stop the one that should not "
                    f"be running")))

        # Reported by the collector itself and stored, but raised by nothing
        # until now - a trap listener that failed to bind ran silently with
        # no traps. Not while a duplicate is seen: two processes alternate
        # heartbeats, so the error comes and goes every tick and the alarm
        # flapped - and the duplicate is the cause, already raised above.
        duplicate = (c.duplicate_age_s is not None
                     and c.duplicate_age_s < DUPLICATE_WINDOW_S)
        if c.config_error and not duplicate:
            out.append(Finding(
                alarm_type="collector_misconfigured", instance=c.collector_id,
                severity=WARNING,
                message=(
                    f"Collector {c.collector_id} could not apply part of its "
                    f"configuration and is running without it: "
                    f"{c.config_error[:240]}")))

        # docs/26 Phase 5: capacity. Shed polls are the unambiguous case -
        # the queue was full and the poll was never made - so they raise
        # MAJOR on their own, whatever the busy figure says.
        if (c.capacity_busy_pct is not None
                and c.capacity_window_s >= CAPACITY_MIN_WINDOW_S):
            mins = c.capacity_window_s / 60
            late = (f" {c.capacity_late} polls started more than 5 s late."
                    if c.capacity_late else "")
            if c.capacity_shed > 0:
                out.append(Finding(
                    alarm_type="collector_capacity_high", instance=c.collector_id,
                    severity=MAJOR, value=float(c.capacity_shed),
                    message=(
                        f"Collector {c.collector_id} shed {c.capacity_shed} "
                        f"polls in the last {mins:.0f} minutes because its "
                        f"queue was full - those polls were never made. Its "
                        f"poll workers are {c.capacity_busy_pct:.0f}% busy."
                        f"{late} Add a collector to its pool or raise its "
                        f"worker count")))
            elif c.capacity_busy_pct >= CAPACITY_WARN_PCT:
                out.append(Finding(
                    alarm_type="collector_capacity_high", instance=c.collector_id,
                    severity=(MAJOR if c.capacity_busy_pct >= CAPACITY_MAJOR_PCT
                              else WARNING),
                    value=c.capacity_busy_pct, threshold=CAPACITY_WARN_PCT,
                    message=(
                        f"Collector {c.collector_id}'s poll workers have been "
                        f"{c.capacity_busy_pct:.0f}% busy over the last "
                        f"{mins:.0f} minutes.{late} It has little headroom "
                        f"left for a slow device or a retry storm")))

    # --- collector pools (docs/26 Phase 6) -------------------------------------
    #
    # min_members is only ever meaningful once an operator has set it above
    # the default of 1 - a pool nobody asked for N+1 on reporting "below
    # minimum" at 1-of-1 would be every single-collector pool in the
    # estate, permanently, which is not a finding, it is the entire
    # deployment shape most sites actually run.
    for pool in signals.pools:
        if pool.min_members <= 1:
            continue
        if pool.healthy_accepting_members < pool.min_members:
            out.append(Finding(
                alarm_type="pool_below_min_members", instance=pool.pool_id,
                severity=MAJOR if pool.healthy_accepting_members == 0 else WARNING,
                value=float(pool.healthy_accepting_members),
                threshold=float(pool.min_members),
                message=(
                    f"Pool {pool.name} has {pool.healthy_accepting_members} "
                    f"of its required {pool.min_members} collectors healthy "
                    f"and accepting work. "
                    + ("No collector can currently poll this pool at all."
                       if pool.healthy_accepting_members == 0 else
                       "It is running with less redundancy than configured."))))

    # --- pool rate budgets (docs/26 Phase 5) ------------------------------------
    for pool in signals.pools:
        budget = pool.rate_budget_points_per_s
        if not budget or pool.points_per_s is None:
            continue
        fraction = pool.points_per_s / budget
        if fraction >= BUDGET_WARN_FRACTION:
            over = fraction >= BUDGET_OVER_FRACTION
            out.append(Finding(
                alarm_type="pool_rate_budget_exceeded", instance=pool.pool_id,
                severity=MAJOR if over else WARNING,
                value=round(pool.points_per_s, 1), threshold=float(budget),
                message=(
                    f"Pool {pool.name} is polling {pool.points_per_s:.0f} "
                    f"points/s against a rate budget of {budget:.0f} "
                    f"({fraction * 100:.0f}%). "
                    + ("The target network is getting more than it agreed "
                       "to take. Collectors hold the pool to its budget, so "
                       "one is not enforcing it - most likely a collector "
                       "built before enforcement; check the Releases page."
                       if over else
                       "Collectors hold the pool to its budget by deferring "
                       "polls, so at this level the budget, not the poll "
                       "intervals, decides how fresh this pool's data is. "
                       "Raise the budget if the network can take it, or "
                       "lengthen the intervals."))))

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

    # --- outbound integrations ------------------------------------------------
    #
    # An integration is a promise that somebody else will be told. Every way it
    # can quietly stop keeping that promise belongs on this console, because
    # the symptom is silence and silence is exactly what nobody investigates.
    for integration in signals.integrations:
        days = integration.days_left
        if days is not None and days <= CREDENTIAL_WARNING_DAYS:
            severity = MAJOR if days <= CREDENTIAL_MAJOR_DAYS else WARNING
            if days < 0:
                severity = CRITICAL
                detail = (f"expired {abs(days):.0f} days ago. No ticket has "
                          f"been opened since")
            else:
                detail = f"expires in {days:.0f} days"
            out.append(Finding(
                alarm_type="integration_credential_expiring",
                instance=integration.id, severity=severity,
                value=round(days, 1), threshold=CREDENTIAL_WARNING_DAYS,
                message=(
                    f"The credential for the {integration.name} integration "
                    f"{detail}. Atlassian Cloud API tokens last at most a "
                    f"year; when this one lapses, alarms stop reaching the "
                    f"service desk and nothing else will say so")))

        webhook_days = integration.webhook_days_left
        if webhook_days is not None and webhook_days <= WEBHOOK_WARNING_DAYS:
            if webhook_days < 0:
                severity, detail = MAJOR, (
                    f"lapsed {abs(webhook_days):.0f} days ago. Nothing from "
                    f"Jira has reached this platform since")
            else:
                severity = (MAJOR if webhook_days <= WEBHOOK_MAJOR_DAYS
                            else WARNING)
                detail = f"expires in {webhook_days:.0f} days"
            out.append(Finding(
                alarm_type="integration_webhook_expiring",
                instance=integration.id, severity=severity,
                value=round(webhook_days, 1), threshold=WEBHOOK_WARNING_DAYS,
                message=(
                    f"The Jira webhook registration for {integration.name} "
                    f"{detail}, and this platform has not been able to renew "
                    f"it. When it lapses, closing a ticket stops "
                    f"acknowledging its alarm and nothing else will say so")))

        if integration.dead_letters >= DEAD_LETTER_MAJOR:
            out.append(Finding(
                alarm_type="integration_degraded", instance=integration.id,
                severity=MAJOR, value=integration.dead_letters,
                threshold=DEAD_LETTER_MAJOR,
                message=(
                    f"{integration.dead_letters} outbound messages to "
                    f"{integration.name} have been given up on. Conditions "
                    f"that should have raised a ticket did not, and the "
                    f"reason is on each one in Settings > Integrations")))

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
