"""A trap is advisory. The measurement decides when the condition is over.

A trap is one UDP datagram with no retry and no acknowledgement, so the clear
that ends a condition is exactly as losable as the raise that started it - and
losing the clear is the one that hurts, because the alarm then stands with
nothing able to resolve it.

It happened here rather than in theory: the simulator fired CPUNormal at
14:14:12 into the 33 seconds the collector was down for a restart. The datagram
hit a closed port, the sending rule engine had already flipped out of alert and
never sent another, and three alarms sat open on a server whose CPU this
platform could see was 39.9%.

Two ways the poll gets the last word, and one safety condition that matters
more than either:

* the measurement CONTESTS the alarm - a rule covers the metric on that kind of
  device and telemetry has been past its clear point;
* nothing has re-asserted it and no rule can speak for it, so it ages out;
* but ONLY while the device is still delivering telemetry. "We stopped hearing
  about the condition" and "we stopped hearing anything" are the same row in
  the alarm table, and only the first means recovery.
"""

from __future__ import annotations

import inspect

import pytest

from app.alarms import reconcile, service


def sql(name: str) -> str:
    return str(getattr(reconcile, name))


# ------------------------------------------------------- what may be reconciled


def test_threshold_alarms_are_left_to_their_own_rule():
    """The rule engine already clears those, with its own hysteresis.

    Reconciling them here would be a second opinion on a question that has a
    first one, and the two would race on every sweep.
    """
    assert "threshold" not in reconcile.RECONCILABLE_SOURCES
    assert "snmp_trap" in reconcile.RECONCILABLE_SOURCES


def test_only_open_alarms_are_touched():
    for name in ("_MEASURED_CLEAR", "_AGED_OUT"):
        assert "a.state <> 'CLEARED'" in sql(name)


# --------------------------------------------------- the measurement contests


def test_every_one_of_the_newest_samples_must_be_in_the_clear_band():
    """One reading below a threshold is a dip, not a recovery - and one above
    it, an hour ago, is not a reason to keep an alarm.

    The extreme of the LAST `need` samples is what decides: max for a `>` rule,
    min for a `<` one. Taking the extreme over the whole window instead sounds
    safer and is not - the readings that RAISED the alarm are in that window
    too, so the alarm would stand until they aged out of it, half an hour after
    the condition ended.
    """
    s = sql("_MEASURED_CLEAR")
    assert "row_number() OVER (PARTITION BY c.id ORDER BY t.ts DESC)" in s
    assert "WHERE rn <= need" in s
    assert "max(value)" in s and "min(value)" in s
    assert "operator = '>' AND hi < clear_threshold" in s
    assert "operator = '<' AND lo > clear_threshold" in s


def test_the_window_still_bounds_how_old_evidence_may_be():
    """A device that fell silent an hour ago must not be cleared by whatever
    it last happened to say."""
    assert "t.ts > now() - make_interval(secs => :window_s)" in sql("_MEASURED_CLEAR")


def test_a_clear_needs_enough_samples_to_be_evidence():
    """A single reading after a long silence is not a trend."""
    s = sql("_MEASURED_CLEAR")
    assert "samples >= need" in s
    assert "clear_dwell_samples" in s


def test_the_rule_must_apply_to_this_kind_of_device():
    """cpu_high is scoped to network gear; it cannot speak for a server.

    Without the device-type test a switch's rule would be used to clear a
    server's alarm, at a threshold chosen for a control plane.
    """
    for name in ("_MEASURED_CLEAR", "_AGED_OUT"):
        assert "cardinality(r.device_types) = 0" in sql(name)
        assert "d.device_type = ANY(r.device_types)" in sql(name)


def test_the_clear_point_is_used_rather_than_the_raise_point():
    """Clearing at the raise threshold is how an alarm flaps.

    The gap between them is the hysteresis; reconciliation has to respect it
    like any other clear.
    """
    s = sql("_MEASURED_CLEAR")
    assert "clear_threshold" in s
    assert "r.threshold" not in s


# ------------------------------------------------------------- ageing out


def test_ageing_only_applies_where_no_rule_can_speak():
    """Otherwise it would pre-empt the measurement with a timer."""
    assert "NOT EXISTS" in sql("_AGED_OUT")
    assert "alarm_rule r" in sql("_AGED_OUT")


def test_a_device_that_went_dark_keeps_its_alarms():
    """The safety condition, and the reason this is not just a timeout.

    Ageing an alarm out because nothing repeated it, on a device that has
    stopped speaking entirely, would delete the evidence exactly when the
    condition is most likely still true.
    """
    s = sql("_AGED_OUT")
    assert "FROM telemetry_sample t" in s
    assert "t.ts > now() - make_interval(secs => :fresh_s)" in s


def test_the_grace_is_several_re_assertion_intervals():
    """Gear re-sends every few minutes while a condition holds.

    A grace shorter than a couple of those would clear live alarms between
    their own repeats.
    """
    assert reconcile.REASSERT_GRACE_S >= 900


def test_freshness_covers_the_slowest_poll_profile():
    """The 600 s network profile is the slowest thing in this fleet.

    A freshness window under it would call every switch "gone dark" between
    two perfectly normal polls, and stop ageing anything on them.
    """
    assert reconcile.SEEING_IT_S >= 600


# --------------------------------------------------------------- the sweep


def test_the_sweep_releases_symptoms_like_any_other_clear():
    """A folded band or a dependency symptom has to come back into view.

    A reconciled root that left its symptoms hidden would take a real, still
    open condition off the console with it.
    """
    src = inspect.getsource(service.AlarmService.sweep_trap_reconciliation)
    assert "release_symptoms" in src


def test_the_sweep_records_why_it_cleared():
    """An alarm that vanishes with no reason is worse than one that stays."""
    src = inspect.getsource(service.AlarmService.sweep_trap_reconciliation)
    assert "record_history" in src
    assert "reconciliation" in src


def test_the_worker_runs_it_on_the_sweep_cadence():
    from app.ingest import worker

    src = inspect.getsource(worker.IngestWorker._maybe_sweep_staleness)
    assert "sweep_trap_reconciliation" in src


@pytest.mark.parametrize("fn,params", [
    ("measured_clear", {"window_s"}),
    ("aged_out", {"grace_s", "fresh_s"}),
])
def test_the_windows_are_arguments_rather_than_literals(fn, params):
    """So a deployment with slower gear can widen them without a code change."""
    sig = inspect.signature(getattr(reconcile, fn))
    assert params <= set(sig.parameters)


# ------------------------------------------- the threshold the device declared


def test_the_devices_own_threshold_is_used_when_no_rule_has_one():
    """The case that started all of this.

    A server's CPU has no rule, deliberately - a busy server is not a fault -
    so before this the platform could only resolve a CPU trap with another trap
    or with a timer. The trap said "93, limit 90" and both numbers were thrown
    away on the way in.
    """
    s = sql("_MEASURED_CLEAR")
    assert "coalesce(r.metric_key, a.metric_key)" in s
    assert "a.threshold * (1 - CAST(:margin AS numeric))" in s


def test_a_rule_still_wins_when_it_has_an_opinion():
    """An operator tuned that number and thought about hysteresis; a margin
    invented here has done neither."""
    s = sql("_MEASURED_CLEAR")
    assert "coalesce(r.clear_threshold," in s
    head = s[s.index("coalesce(r.clear_threshold,"):][:120]
    assert head.index("r.clear_threshold") < head.index("a.threshold")


def test_something_must_name_both_a_metric_and_a_limit():
    """Half a measurement cannot contest anything."""
    s = sql("_MEASURED_CLEAR")
    assert "coalesce(r.metric_key, a.metric_key) IS NOT NULL" in s
    assert "r.clear_threshold IS NOT NULL OR a.threshold IS NOT NULL" in s


def test_the_margin_is_a_stand_in_for_hysteresis_not_a_free_pass():
    """Clearing the instant a reading dips under its own threshold would flap
    the alarm on a value hovering at the line."""
    assert 0 < reconcile.CLEAR_MARGIN < 0.2


def test_the_timer_only_gets_what_nothing_can_measure():
    """Once a reading CAN decide an alarm, the reading decides - not the clock.

    Leaving the timer able to reach measurable alarms would let it close one
    early, on silence, when the measurement was about to disagree.
    """
    aged = sql("_AGED_OUT")
    assert "a.threshold IS NULL" in aged
    # A rule that can decide the alarm outranks the clock too.
    assert "NOT EXISTS" in aged
    assert "r.clear_threshold IS NOT NULL" in aged


def test_the_two_reconcilers_leave_no_alarm_unreachable():
    """Their gates have to be complements, not merely different.

    measured_clear acts when a limit exists - from the rule or from the device.
    So the timer's gate must be that same question negated. It was not: the
    timer asked whether the alarm named a METRIC, which is a different question,
    and the difference was a hole. A trap that named a metric and carried no
    limit - outlet_current_high, whose vendors send a reading and no threshold -
    was excluded here for naming a metric and excluded there for having no
    limit. Nothing could close it at all, and one sat open for over ninety
    minutes on a healthy strip that was reporting the whole time.
    """
    measured, aged = sql("_MEASURED_CLEAR"), sql("_AGED_OUT")
    assert "r.clear_threshold IS NOT NULL OR a.threshold IS NOT NULL" in measured
    assert "a.threshold IS NULL" in aged
    # Neither may gate on metric_key: naming a measurement is not the same as
    # being able to decide one, and conflating them is what opened the hole.
    assert "a.metric_key IS NULL" not in aged


def test_the_reason_says_whose_threshold_was_used():
    """An operator reading the history should not have to guess whether the
    number came from their rule or from the device."""
    from_rule = reconcile.measured_reason(
        {"metric_key": "cpu_utilization", "worst": 52.0,
         "clear_threshold": 70.0, "from_rule": True})
    from_device = reconcile.measured_reason(
        {"metric_key": "cpu_utilization", "worst": 52.0,
         "clear_threshold": 85.5, "from_rule": False})
    assert "rule's" in from_rule
    assert "device's own" in from_device


def test_only_readings_taken_after_the_condition_count_as_recovery():
    """The bug this caught, live, within a minute of shipping.

    An injected CPU fault raised at 08:20:42 was cleared at 08:20:47 - five
    seconds later - by a 63.2% reading taken at 08:17:50, three minutes before
    the CPU ever climbed. Polls run every minute or two; a trap arrives the
    instant the condition starts, so the newest sample in the window is very
    often one from BEFORE the fault. That reading says nothing about whether
    the fault has ended.

    Anchoring on last_seen rather than first_seen also means a device that
    keeps re-asserting the condition keeps resetting the evidence clock, which
    is right: the newest assertion is the one that has to be outlived.
    """
    assert "t.ts > c.last_seen" in sql("_MEASURED_CLEAR")


def test_the_margin_parameter_is_cast_rather_than_inferred():
    """asyncpg sends parameters typed, and Postgres infers this one from its
    neighbour: `1 - :margin` types the parameter int4, 0.05 becomes 0, and the
    margin disappears without an error anywhere.

    It shipped, cleared two live alarms at exactly their raise threshold, and
    every test still passed - including running the statement by hand through
    psql, which sends parameters untyped and infers numeric. Only the real
    driver reproduces it.
    """
    assert "CAST(:margin AS numeric)" in sql("_MEASURED_CLEAR")



# ------------------------------------------------ the condition that is a STATE
#
# Three CRAHs an operator stopped raised plant_unit_stopped, stayed stopped,
# and had all three alarms aged out thirty minutes later - while the polled
# boolean was still reporting "not running" every heartbeat and the thermal
# page was still showing them Stopped. The alarm list said the hall was fine
# and the same platform, on the same screen, said three units were down.


def test_the_stopped_unit_alarm_is_backed_by_the_polled_state():
    """Any instance, because the point is named differently on every machine:
    Unit_Running on a CRAH and a CDU, Chiller_Running, Run_Status on a pump,
    Fan_Status on a tower cell, Status_Modulating on a valve. Naming one of
    them left the other five unshielded."""
    assert reconcile.STATE_BACKED["plant_unit_stopped"] == (
        "equipment_state", reconcile.ANY_INSTANCE, False)


def test_every_plant_condition_a_trap_names_has_a_polled_backstop():
    """The estate's plant traps, and the boolean point each one is really
    reporting. A trap-only condition is one lost datagram from silence."""
    for alarm_type in ("chiller_high_pressure", "chiller_flow_loss",
                       "chiller_low_evap_temp", "tower_high_vibration",
                       "tower_low_basin", "pump_fault", "pump_low_flow",
                       "valve_actuator_fault", "crah_airflow_loss",
                       "crah_high_temp", "crah_filter_dirty",
                       "plant_unit_stopped"):
        assert alarm_type in reconcile.STATE_BACKED, alarm_type


def test_the_generic_equipment_alarm_reads_its_own_point():
    """It still carries every point with no trap of its own - phase loss, a
    battery fault, a CDU's leak - and those are filed under the point they
    came from, so the alarm's own instance is the one to read back."""
    assert reconcile.STATE_BACKED["equipment_alarm"] == (
        "alarm_state", reconcile.ANY_INSTANCE, True)


def test_a_state_backed_alarm_is_never_aged_out_by_the_timer():
    """The timer is for what nothing can measure. A boolean IS a measurement,
    and it can hold the alarm open as well as close it - so the type is out of
    the timer's reach in both directions, and cannot be closed on a clock
    while the platform is still being told the machine is off."""
    assert "NOT (a.alarm_type = ANY(:state_types))" in sql("_AGED_OUT")


def test_the_timer_is_given_the_types_it_must_skip():
    """A guard in the SQL that nothing binds is a guard that does nothing."""
    src = inspect.getsource(reconcile.aged_out)
    assert '"state_types": list(STATE_BACKED)' in src


def test_the_state_read_is_per_alarm_point_not_per_device():
    """One boolean metric carries every alarm point on a machine - a CRAH
    publishes alarm_state four times over, once per point - so a read that
    ignored the instance would answer "is this alarm still true" with
    whichever sibling was written last."""
    q = sql("_STATE")
    assert "l.instance = p.instance" in q
    assert "DISTINCT ON (tb.device_id, m.key, tb.instance)" in q


def test_a_stale_boolean_speaks_for_nothing():
    """Booleans are stored on change plus a heartbeat, not on every poll. Past
    two heartbeats the state is not evidence in either direction, and the
    alarm is left exactly as it was."""
    assert "tb.ts > now() - make_interval(secs => :fresh_s)" in sql("_STATE")
    assert reconcile.STATE_FRESH_S >= 1800


@pytest.mark.asyncio
async def test_only_the_rows_whose_condition_ended_are_cleared():
    """The same read serves both halves: it shields the alarms whose state
    still holds and closes the ones whose state has moved on. Only the second
    set may be handed to the clear."""
    rows = [
        {"id": "1", "device_id": "d1", "device_name": "CRAH1", "ended": True,
         "alarm_type": "plant_unit_stopped", "severity": "MAJOR",
         "metric_key": "equipment_state", "instance": "Unit_Running",
         "state": True},
        {"id": "2", "device_id": "d2", "device_name": "CRAH2", "ended": False,
         "alarm_type": "plant_unit_stopped", "severity": "MAJOR",
         "metric_key": "equipment_state", "instance": "Unit_Running",
         "state": False},
    ]

    class _Result:
        def mappings(self):
            return self

        def all(self):
            return rows

    class _Session:
        async def execute(self, *_a, **_k):
            return _Result()

    out = await reconcile.state_settled(_Session())
    assert [r["device_name"] for r in out] == ["CRAH1"]


def test_the_reason_names_the_point_that_settled_it():
    reason = reconcile.state_reason({
        "metric_key": "equipment_state", "point": "Unit_Running",
        "instance": "", "state": True})
    assert "equipment_state/Unit_Running" in reason
    assert "true" in reason


def test_the_sweep_asks_the_state_before_it_asks_the_clock():
    """Order is the whole fix: a measurement that can decide must decide
    before a timer that cannot."""
    src = inspect.getsource(service.AlarmService.sweep_trap_reconciliation)
    assert src.index("state_settled") < src.index("aged_out")



# ------------------------------------------------- the poll raises it as well
#
# A trap is one datagram in each direction. Losing the CLEAR leaves a false
# alarm somebody can see and argue with; losing the RAISE leaves a stopped
# machine nobody is told about, which is the worse half - and until this, a
# stopped cooling unit could only ever be announced by a trap.


def test_a_stopped_unit_files_at_the_machine_however_it_was_noticed():
    """The alarm key is (device, type, instance). A trap files this under no
    instance and a polled rule would file it under the point it read - so one
    stopped CRAH would sit on the console twice, neither row a duplicate of the
    other as far as the key is concerned."""
    from app.core import alert_taxonomy as tax

    assert tax.alarm_instance("equipment_state", "Unit_Running",
                              "plant_unit_stopped") == ""
    assert tax.alarm_instance("equipment_state", "", "plant_unit_stopped") == ""


def test_the_metric_itself_stays_per_point():
    """It has to. equipment_state carries one run point on a CRAH and three
    separate ones on an ATS - available, on emergency, tie closed - and
    collapsing those would merge two genuinely different states into one."""
    from app.core import alert_taxonomy as tax

    assert tax.alarm_instance("equipment_state", "On_Emergency",
                              "equipment_alarm") == "On_Emergency"
    assert tax.alarm_instance("equipment_state", "Normal_Available") == "Normal_Available"


def test_the_scope_is_decided_per_rule_not_per_sample():
    """One boolean point can feed a rule that files at the machine and another
    that files at the point, so the instance cannot be settled before knowing
    which alarm the reading is being judged for."""
    src = inspect.getsource(service.AlarmService.evaluate_samples)
    assert "rule.alarm_type)" in src
    assert src.index("for rule in self.rules_for_metric") < src.index(
        "alert_taxonomy.alarm_instance")


def test_an_instance_filter_still_matches_what_the_source_called_the_point():
    """The filter names the POINT; the scope names where the alarm is FILED.
    Matching the filter against the filed instance would make a rule that
    files at the machine unable to select the point it reads."""
    src = inspect.getsource(service.AlarmService.evaluate_samples)
    assert "rule.applies_to_instance(raw_instance)" in src
