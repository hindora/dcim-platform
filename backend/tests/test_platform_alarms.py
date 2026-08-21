"""Monitoring the monitoring.

Every test here is a variation on one question: can this platform tell the
difference between a quiet datacenter and a dead pipeline? An alarm system that
cannot is not a degraded alarm system, it is a screen that says everything is
fine no matter what happens.
"""

from __future__ import annotations

from app.alarms import platform as p


def sig(**kw) -> p.Signals:
    """Signals from a healthy platform, overridden per test.

    The healthy baseline has a heartbeat and recent telemetry, because leaving
    those unset would make half these tests pass for the wrong reason.
    """
    base = {"ingest_lag_s": 0.4, "telemetry_age_s": 30.0,
            "telemetry_present": True, "worker_heartbeat_age_s": 5.0,
            "collectors": [p.Collector(collector_id="col-1", heartbeat_age_s=10.0,
                                       endpoints_owned=664)]}
    base.update(kw)
    return p.Signals(**base)


def types(findings: list[p.Finding]) -> set[str]:
    return {f.alarm_type for f in findings}


# --- the exit criterion -------------------------------------------------------

def test_a_healthy_platform_raises_nothing():
    assert p.evaluate(sig()) == []


def test_pipeline_lag_over_a_minute_warns():
    found = p.evaluate(sig(ingest_lag_s=75.0))
    assert types(found) == {"ingest_lag_high"}
    assert found[0].severity == p.WARNING


def test_pipeline_lag_over_five_minutes_is_critical():
    found = p.evaluate(sig(ingest_lag_s=400.0))
    assert found[0].severity == p.CRITICAL
    assert found[0].value == 400.0
    assert found[0].threshold == p.INGEST_LAG_CRITICAL_S


def test_lag_and_freshness_are_not_the_same_number():
    """The distinction the whole metric design rests on.

    Data freshness is bounded by the poll interval even in perfect health: a
    fleet polled every 120 s routinely has a newest sample 100 s old. Judged
    against the 60 s lag threshold that is a permanent alarm on a healthy
    system, which is how alerting gets switched off.
    """
    healthy = sig(ingest_lag_s=0.4, telemetry_age_s=110.0, poll_interval_s=120.0)
    assert p.evaluate(healthy) == []


def test_freshness_alarms_only_after_several_missed_cycles():
    assert types(p.evaluate(sig(telemetry_age_s=400.0, poll_interval_s=120.0))) \
        == {"ingest_stalled"}


# --- absence is not health ----------------------------------------------------

def test_a_missing_worker_heartbeat_is_an_alarm_not_a_skipped_check():
    """Found live: against a worker too old to heartbeat, the age came back
    None, the guard skipped, and the platform announced it was finding nothing
    wrong while unable to see the worker at all."""
    found = p.evaluate(sig(worker_heartbeat_age_s=None))
    assert types(found) == {"ingest_worker_stale"}


def test_a_missing_heartbeat_with_telemetry_flowing_is_a_blind_spot_not_an_outage():
    """Telemetry arriving proves something is draining the stream, whatever it
    reports about itself. Calling that critical failed readiness during a
    version skew where the pipeline was demonstrably healthy - which in a real
    deployment pulls a working API out of the load balancer."""
    found = p.evaluate(sig(worker_heartbeat_age_s=None, telemetry_age_s=9.0))
    assert found[0].severity == p.WARNING
    assert "blind spot" in found[0].message


def test_a_missing_heartbeat_with_nothing_arriving_is_critical():
    found = p.evaluate(sig(worker_heartbeat_age_s=None, telemetry_age_s=4000.0,
                           poll_interval_s=120.0))
    worker = [f for f in found if f.alarm_type == "ingest_worker_stale"]
    assert worker[0].severity == p.CRITICAL


def test_an_empty_telemetry_table_is_an_alarm_not_a_zero():
    found = p.evaluate(sig(telemetry_present=False, telemetry_age_s=None))
    assert "ingest_stalled" in types(found)


def test_no_collectors_at_all_is_an_alarm_when_any_were_expected():
    found = p.evaluate(sig(collectors=[], collectors_expected=1))
    assert "collector_stale" in types(found)


def test_a_collector_that_has_never_beaten_is_stale():
    found = p.evaluate(sig(collectors=[
        p.Collector(collector_id="col-1", heartbeat_age_s=None)]))
    assert "collector_stale" in types(found)


# --- one fault, one alarm -----------------------------------------------------

def test_a_dead_collector_does_not_also_report_degraded_and_stale_assignment():
    """Three alarms for one fault means an operator reads three to find the one
    that matters, and the other two are consequences of the first."""
    found = p.evaluate(sig(collectors=[p.Collector(
        collector_id="col-1", heartbeat_age_s=600.0, publish_dropped=40,
        assignment_age_s=9999.0)]))
    assert types(found) == {"collector_stale"}


def test_dropped_publishes_outrank_a_full_queue():
    """A full queue is a warning about the future; a drop is data that no
    longer exists anywhere. They should not both be reported as one type at two
    severities on the same collector."""
    found = p.evaluate(sig(collectors=[p.Collector(
        collector_id="col-1", heartbeat_age_s=5.0, publish_dropped=3,
        publish_queue_depth=99, publish_queue_capacity=100)]))
    degraded = [f for f in found if f.alarm_type == "collector_degraded"]
    assert len(degraded) == 1
    assert degraded[0].severity == p.MAJOR
    assert "data loss" in degraded[0].message


def test_a_nearly_full_publish_queue_warns():
    found = p.evaluate(sig(collectors=[p.Collector(
        collector_id="col-1", heartbeat_age_s=5.0,
        publish_queue_depth=85, publish_queue_capacity=100)]))
    assert [f.severity for f in found if f.alarm_type == "collector_degraded"] \
        == [p.WARNING]


def test_a_stale_assignment_warns_on_a_live_collector():
    found = p.evaluate(sig(collectors=[p.Collector(
        collector_id="col-1", heartbeat_age_s=5.0, assignment_age_s=600.0)]))
    assert "assignment_stale" in types(found)


def test_a_saturated_pool_alarms_only_when_sustained():
    assert p.evaluate(sig(db_pool_saturated_for_s=5.0)) == []
    assert "db_pool_exhausted" in types(p.evaluate(sig(db_pool_saturated_for_s=45.0)))


# --- lifecycle ----------------------------------------------------------------

def test_findings_that_disappear_are_cleared():
    """Almost none of these conditions produce a recovery event - a collector
    that starts heartbeating again does not announce that it had stopped - so
    clearing is driven by absence."""
    current = p.evaluate(sig())
    open_keys = {("collector_stale", "col-1"), ("ingest_lag_high", "telemetry.v1")}
    _, to_clear = p.diff(current, open_keys)
    assert set(to_clear) == open_keys


def test_a_finding_that_persists_is_not_cleared_and_not_duplicated():
    current = p.evaluate(sig(ingest_lag_s=400.0))
    _, to_clear = p.diff(current, {("ingest_lag_high", "telemetry.v1")})
    assert to_clear == []


def test_the_verdict_reports_the_worst_finding():
    found = p.evaluate(sig(ingest_lag_s=400.0, collectors=[
        p.Collector(collector_id="col-1", heartbeat_age_s=5.0,
                    assignment_age_s=600.0)]))
    verdict = p.summarise(found)
    assert verdict["healthy"] is False
    assert verdict["severity"] == p.CRITICAL
    assert verdict["count"] == 2


def test_a_healthy_verdict_says_so_in_words():
    verdict = p.summarise([])
    assert verdict["healthy"] is True
    assert verdict["severity"] is None
