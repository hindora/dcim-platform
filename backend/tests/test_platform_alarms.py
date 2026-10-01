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


def test_a_lag_spike_that_has_not_persisted_raises_nothing():
    """Added with the dwell: size alone is no longer enough.

    Three raises in one hour on this platform - 89.8 s, 90.8 s, 106.8 s - each
    clearing within two minutes while the consumer was 0.3 s behind the newest
    entry. The queue was working; the banner said the estate was degraded.
    """
    assert [f for f in p.evaluate(sig(ingest_lag_s=400.0, ingest_lag_sustained_s=5.0))
            if f.alarm_type == "ingest_lag_high"] == []


def test_pipeline_lag_over_a_minute_warns():
    found = p.evaluate(sig(ingest_lag_s=75.0, ingest_lag_sustained_s=999.0))
    assert types(found) == {"ingest_lag_high"}
    assert found[0].severity == p.WARNING


def test_pipeline_lag_over_five_minutes_is_critical():
    found = p.evaluate(sig(ingest_lag_s=400.0, ingest_lag_sustained_s=999.0))
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
    current = p.evaluate(sig(ingest_lag_s=400.0, ingest_lag_sustained_s=999.0))
    _, to_clear = p.diff(current, {("ingest_lag_high", "telemetry.v1")})
    assert to_clear == []


def test_the_verdict_reports_the_worst_finding():
    found = p.evaluate(sig(ingest_lag_s=400.0, ingest_lag_sustained_s=999.0, collectors=[
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


# ------------------------------------------------------- outbound integrations
#
# An integration is a promise that somebody else will be told. Every way it can
# quietly stop keeping that promise belongs on this console, because the
# symptom is SILENCE - the row still reads "enabled", the outbox still fills,
# and the first anybody knows is a fault nobody was told about.

def integration(**kw) -> p.Integration:
    base = {"id": "i1", "name": "Acme Jira"}
    base.update(kw)
    return p.Integration(**base)


def test_a_healthy_integration_raises_nothing():
    assert types(p.evaluate(sig(integrations=[integration(days_left=200.0)]))) == set()


def test_no_expiry_recorded_raises_nothing():
    """Atlassian does not expose a token's expiry over the API, so this is a
    date somebody typed in. Absent means unknown, not imminent."""
    assert types(p.evaluate(sig(integrations=[integration()]))) == set()


def test_a_credential_a_month_out_warns():
    found = p.evaluate(sig(integrations=[integration(days_left=20.0)]))
    assert types(found) == {"integration_credential_expiring"}
    assert found[0].severity == p.WARNING


def test_a_credential_a_week_out_is_major():
    found = p.evaluate(sig(integrations=[integration(days_left=5.0)]))
    assert found[0].severity == p.MAJOR


def test_a_credential_that_has_already_lapsed_is_critical():
    """The case that actually happens. Atlassian force-expired the whole
    pre-December-2024 generation of API tokens in spring 2026, and an install
    configured before that simply stopped opening tickets."""
    found = p.evaluate(sig(integrations=[integration(days_left=-3.0)]))
    assert found[0].severity == p.CRITICAL
    assert "expired 3 days ago" in found[0].message


def test_the_expiry_alarm_is_keyed_per_integration():
    """Two Jiras must not clear each other's alarm."""
    found = p.evaluate(sig(integrations=[
        integration(days_left=5.0),
        integration(id="i2", name="Other", days_left=2.0)]))
    assert {f.instance for f in found} == {"i1", "i2"}


def test_one_dead_letter_is_a_mapping_mistake_not_an_outage():
    """One is something a human can fix on the row. It does not deserve a
    console alarm, or the console fills with them."""
    assert types(p.evaluate(sig(integrations=[integration(dead_letters=1)]))) == set()


def test_a_queue_of_dead_letters_means_the_integration_is_broken():
    found = p.evaluate(sig(integrations=[integration(dead_letters=7)]))
    assert types(found) == {"integration_degraded"}
    assert "7 outbound messages" in found[0].message


def test_both_conditions_can_be_open_on_one_integration():
    found = p.evaluate(sig(integrations=[
        integration(days_left=2.0, dead_letters=9)]))
    assert types(found) == {"integration_credential_expiring",
                            "integration_degraded"}


def test_every_integration_finding_declares_its_type():
    """`PLATFORM_ALARM_TYPES` is what the API uses to separate platform alarms
    from device alarms without string-matching on prefixes, so a type missing
    from it is an alarm that reads as a device fault on a device that does not
    exist."""
    found = p.evaluate(sig(integrations=[integration(days_left=-1.0,
                                                     dead_letters=9)]))
    assert {f.alarm_type for f in found} <= set(p.PLATFORM_ALARM_TYPES)


def test_a_hand_registered_webhook_never_expires_and_never_alarms():
    """Jira Cloud restricts the webhook REST API to Connect and OAuth apps, so
    an install using an API token registers by hand - and a hand-registered
    webhook has no deadline. None here means "nothing to watch", not
    "overdue"."""
    assert types(p.evaluate(sig(integrations=[integration()]))) == set()


def test_a_dynamic_registration_about_to_lapse_warns():
    found = p.evaluate(sig(integrations=[integration(webhook_days_left=4.0)]))
    assert types(found) == {"integration_webhook_expiring"}
    assert found[0].severity == p.WARNING


def test_a_registration_lapsing_within_two_days_is_major():
    found = p.evaluate(sig(integrations=[integration(webhook_days_left=1.0)]))
    assert found[0].severity == p.MAJOR


def test_a_lapsed_registration_says_what_it_costs():
    """Nothing errors when a registration expires - no bounce, no log line.
    Closing a ticket simply stops acknowledging its alarm."""
    found = p.evaluate(sig(integrations=[integration(webhook_days_left=-6.0)]))
    assert found[0].severity == p.MAJOR
    assert "lapsed 6 days ago" in found[0].message
    assert "stops acknowledging" in found[0].message


def test_the_webhook_alarm_is_keyed_per_integration():
    found = p.evaluate(sig(integrations=[
        integration(webhook_days_left=1.0),
        integration(id="i2", name="Other", webhook_days_left=0.5)]))
    assert {f.instance for f in found} == {"i1", "i2"}


def test_a_credential_and_a_webhook_can_both_be_expiring():
    found = p.evaluate(sig(integrations=[
        integration(days_left=3.0, webhook_days_left=1.0)]))
    assert types(found) == {"integration_credential_expiring",
                            "integration_webhook_expiring"}


# --- collector pools (docs/26 Phase 6) -------------------------------------

def pool(**kw) -> p.Pool:
    base = {"pool_id": "pool-1", "name": "DC1/IT-OOB", "min_members": 2,
           "healthy_accepting_members": 2}
    base.update(kw)
    return p.Pool(**base)


def test_a_pool_at_full_strength_raises_nothing():
    assert p.evaluate(sig(pools=[pool()])) == []


def test_a_single_member_pool_never_alarms_on_min_members():
    """min_members = 1 (the default for every pool an operator has not
    explicitly asked for N+1 on) must never alarm just for being at 1-of-1
    - that is every ordinary single-collector pool in the estate."""
    found = p.evaluate(sig(pools=[pool(min_members=1, healthy_accepting_members=1)]))
    assert types(found) == set()


def test_a_pool_missing_one_of_two_members_warns():
    found = p.evaluate(sig(pools=[pool(healthy_accepting_members=1)]))
    assert types(found) == {"pool_below_min_members"}
    assert found[0].severity == p.WARNING
    assert found[0].instance == "pool-1"


def test_a_pool_with_no_healthy_members_at_all_is_major():
    found = p.evaluate(sig(pools=[pool(healthy_accepting_members=0)]))
    assert found[0].severity == p.MAJOR
    assert "No collector can currently poll this pool at all" in found[0].message


def test_pool_alarms_are_keyed_per_pool():
    found = p.evaluate(sig(pools=[
        pool(pool_id="pool-1", healthy_accepting_members=1),
        pool(pool_id="pool-2", name="DC2/BMS", healthy_accepting_members=0)]))
    assert {f.instance for f in found} == {"pool-1", "pool-2"}


# --- version skew (docs/26 Phase 7) -----------------------------------------

def test_a_current_collector_raises_no_skew_alarm():
    found = p.evaluate(sig(platform_version="2.5.0",
                           collectors=[p.Collector(collector_id="col-1",
                                                   heartbeat_age_s=10.0, version="2.5.0")]))
    assert "collector_outdated" not in types(found)


def test_a_one_generation_behind_collector_raises_nothing_either():
    """N-1 is SUPPORTED, not OUTDATED - only N-2 and older alarm."""
    found = p.evaluate(sig(platform_version="2.5.0",
                           collectors=[p.Collector(collector_id="col-1",
                                                   heartbeat_age_s=10.0, version="2.4.0")]))
    assert "collector_outdated" not in types(found)


def test_a_two_generation_behind_collector_warns():
    found = p.evaluate(sig(platform_version="2.5.0",
                           collectors=[p.Collector(collector_id="col-1",
                                                   heartbeat_age_s=10.0, version="2.3.0")]))
    assert types(found) == {"collector_outdated"}
    assert found[0].severity == p.WARNING
    assert "not being given new endpoints" in found[0].message


def test_a_three_generation_behind_collector_is_major_not_hard_blocked():
    found = p.evaluate(sig(platform_version="2.5.0",
                           collectors=[p.Collector(collector_id="col-1",
                                                   heartbeat_age_s=10.0, version="2.2.0")]))
    assert found[0].severity == p.MAJOR
    assert "does not hard-block" in found[0].message


def test_a_dev_build_never_raises_a_skew_alarm():
    found = p.evaluate(sig(platform_version="2.5.0",
                           collectors=[p.Collector(collector_id="col-1",
                                                   heartbeat_age_s=10.0, version="dev")]))
    assert "collector_outdated" not in types(found)


def test_with_no_platform_version_set_skew_never_alarms():
    """A dev checkout with no real platform_version must not spuriously
    warn about every collector's version, since there is no "N" to measure
    distance from."""
    found = p.evaluate(sig(platform_version="",
                           collectors=[p.Collector(collector_id="col-1",
                                                   heartbeat_age_s=10.0, version="0.1.0")]))
    assert "collector_outdated" not in types(found)


def test_skew_alarms_are_keyed_per_collector():
    found = p.evaluate(sig(platform_version="2.5.0", collectors=[
        p.Collector(collector_id="col-1", heartbeat_age_s=10.0, version="2.3.0"),
        p.Collector(collector_id="col-2", heartbeat_age_s=10.0, version="2.5.0")]))
    assert {f.instance for f in found} == {"col-1"}


# --- capacity (docs/26 Phase 5) ------------------------------------------------

def busy(pct, *, window_s=300.0, shed=0, late=0, cid="col-1"):
    return p.Collector(collector_id=cid, heartbeat_age_s=10.0, endpoints_owned=664,
                       capacity_busy_pct=pct, capacity_window_s=window_s,
                       capacity_shed=shed, capacity_late=late)


def test_a_collector_under_the_line_raises_nothing():
    assert p.evaluate(sig(collectors=[busy(84.9)])) == []


def test_eighty_five_percent_busy_warns():
    found = p.evaluate(sig(collectors=[busy(85.0)]))
    assert types(found) == {"collector_capacity_high"}
    assert found[0].severity == p.WARNING
    assert found[0].threshold == p.CAPACITY_WARN_PCT


def test_ninety_five_percent_busy_is_major():
    found = p.evaluate(sig(collectors=[busy(96.0, late=12)]))
    assert found[0].severity == p.MAJOR
    assert "12 polls started more than 5 s late" in found[0].message


def test_shed_polls_are_major_however_idle_the_workers_look():
    """A shed poll was never made. That is capacity lost outright, and the
    busy average over five minutes can hide the burst that caused it."""
    found = p.evaluate(sig(collectors=[busy(40.0, shed=3)]))
    assert types(found) == {"collector_capacity_high"}
    assert found[0].severity == p.MAJOR
    assert "never made" in found[0].message


def test_a_collector_that_never_reported_capacity_is_not_judged():
    """An older collector, or one with a single reading, sends nothing -
    which is silence, not a zero load and not a full one."""
    found = p.evaluate(sig(collectors=[p.Collector(
        collector_id="col-1", heartbeat_age_s=10.0, endpoints_owned=664)]))
    assert found == []


def test_a_window_shorter_than_a_minute_is_not_judged():
    assert p.evaluate(sig(collectors=[busy(100.0, window_s=20.0)])) == []


def test_a_stale_collector_raises_stale_not_capacity():
    c = busy(99.0)
    c.heartbeat_age_s = 900.0
    assert types(p.evaluate(sig(collectors=[c]))) == {"collector_stale"}


def test_a_pool_within_its_budget_raises_nothing():
    found = p.evaluate(sig(pools=[pool(rate_budget_points_per_s=400.0,
                                       points_per_s=339.0)]))
    assert found == []


def test_a_pool_at_eighty_five_percent_of_budget_warns():
    found = p.evaluate(sig(pools=[pool(rate_budget_points_per_s=400.0,
                                       points_per_s=340.0)]))
    assert types(found) == {"pool_rate_budget_exceeded"}
    assert found[0].severity == p.WARNING


def test_a_pool_over_budget_is_major_and_says_a_collector_is_not_enforcing():
    found = p.evaluate(sig(pools=[pool(rate_budget_points_per_s=400.0,
                                       points_per_s=520.0)]))
    assert found[0].severity == p.MAJOR
    assert "not enforcing" in found[0].message


def test_a_pool_held_at_its_budget_is_a_warning_not_a_failure_to_enforce():
    """Enforced, a pool sits at its budget with the token bucket's burst
    averaging a little over it - that is the budget working."""
    found = p.evaluate(sig(pools=[pool(rate_budget_points_per_s=400.0,
                                       points_per_s=406.0)]))
    assert found[0].severity == p.WARNING and "decides how fresh" in found[0].message


def test_no_budget_or_no_measurement_means_no_budget_alarm():
    assert p.evaluate(sig(pools=[pool(points_per_s=9999.0)])) == []
    assert p.evaluate(sig(pools=[pool(rate_budget_points_per_s=10.0)])) == []


def test_capacity_alarm_types_are_classified():
    from app.core import alert_taxonomy as tax
    for t in ("collector_capacity_high", "pool_rate_budget_exceeded",
              "pool_below_min_members", "collector_outdated"):
        assert t in p.PLATFORM_ALARM_TYPES
        assert tax.BY_ALARM_TYPE[t] == tax.VISIBILITY


# --- duplicate identity and misconfiguration -----------------------------------

def test_two_processes_under_one_identity_is_major():
    c = p.Collector(collector_id="col-1", heartbeat_age_s=5.0, endpoints_owned=95,
                    duplicate_age_s=20.0)
    found = p.evaluate(sig(collectors=[c]))
    assert types(found) == {"collector_duplicate"}
    assert found[0].severity == p.MAJOR and "polled twice" in found[0].message


def test_an_old_duplicate_sighting_has_aged_out():
    c = p.Collector(collector_id="col-1", heartbeat_age_s=5.0, endpoints_owned=95,
                    duplicate_age_s=p.DUPLICATE_WINDOW_S + 1)
    assert p.evaluate(sig(collectors=[c])) == []


def test_a_reported_config_error_is_raised_not_just_stored():
    err = "trap listener on 0.0.0.0:11622: bind: address already in use"
    c = p.Collector(collector_id="col-1", heartbeat_age_s=5.0, endpoints_owned=95,
                    config_error=err)
    found = p.evaluate(sig(collectors=[c]))
    assert types(found) == {"collector_misconfigured"}
    assert found[0].severity == p.WARNING and "address already in use" in found[0].message


def test_a_duplicate_suppresses_the_misconfiguration_it_causes():
    """Two processes alternate heartbeats, so config_error comes and goes every
    tick; raising it flapped the alarm. The duplicate is the root cause."""
    c = p.Collector(collector_id="col-1", heartbeat_age_s=5.0, endpoints_owned=95,
                    duplicate_age_s=10.0, config_error="bind: address already in use")
    assert types(p.evaluate(sig(collectors=[c]))) == {"collector_duplicate"}
