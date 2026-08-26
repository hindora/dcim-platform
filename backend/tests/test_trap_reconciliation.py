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


def test_the_whole_window_must_be_in_the_clear_band():
    """One reading below a threshold is a dip, not a recovery.

    Clearing on the latest sample would flap an alarm that a rule's own clear
    dwell exists to hold, so the extreme of the window is what is tested:
    max for a `>` rule, min for a `<` one.
    """
    s = sql("_MEASURED_CLEAR")
    assert "max(t.value)" in s and "min(t.value)" in s
    assert "operator = '>' AND hi < clear_threshold" in s
    assert "operator = '<' AND lo > clear_threshold" in s


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
