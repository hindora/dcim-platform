"""Which conditions earn a ticket, and why the ones that do not, do not.

The reason strings are tested as hard as the verdicts. A policy that only
returns False is unusable in a settings page: the operator's question is never
"did it match" but "why is nothing coming through", and answering that by
reading the policy back to them is how support tickets are made.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.integrations import policy
from app.integrations.config import SEVERITY_RANK, resolved
from app.repositories import alarms as alarm_repo

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)

DEFAULT = resolved({})["policy"]


def alarm(**over):
    base = {
        "id": "a1", "device_id": "d1", "alarm_type": "inlet_temp_high",
        "instance": "", "severity": "MAJOR", "category": "environmental",
        "response_class": "alarm", "is_symptom": False, "shelved": False,
        "first_seen": NOW - timedelta(hours=1),
    }
    base.update(over)
    return base


# ------------------------------------------------------------ the clauses

def test_a_major_root_cause_alarm_is_ticketed():
    assert policy.explain(alarm(), DEFAULT, now=NOW) == policy.MATCH
    assert policy.matches(alarm(), DEFAULT, now=NOW)


def test_an_alert_is_not_ticketed():
    """`alert` is informational and belongs to whoever schedules the work.
    Mirroring it throws away the one distinction the taxonomy exists for."""
    why = policy.explain(alarm(response_class="alert"), DEFAULT, now=NOW)
    assert "response class" in why and "alert" in why


def test_a_warning_is_below_the_default_floor():
    why = policy.explain(alarm(severity="WARNING"), DEFAULT, now=NOW)
    assert why == "severity WARNING is below MAJOR"


def test_critical_clears_a_major_floor():
    assert policy.matches(alarm(severity="CRITICAL"), DEFAULT, now=NOW)


def test_an_unknown_severity_is_excluded_rather_than_admitted():
    """The safe direction. A severity this platform does not recognise is not
    something to page a service desk about."""
    assert policy.explain(alarm(severity="WEIRD"), DEFAULT, now=NOW)


def test_a_symptom_is_not_ticketed():
    """One OOB switch failure is one incident with twenty suppressed
    symptoms. An exporter that ignores `is_symptom` un-collapses exactly what
    correlation achieved, and does it where nobody can see the root."""
    why = policy.explain(alarm(is_symptom=True), DEFAULT, now=NOW)
    assert "symptom" in why


def test_a_shelved_alarm_is_not_ticketed():
    """Planned work does not page anyone and does not open tickets either -
    the maintenance window already has a change record."""
    why = policy.explain(alarm(shelved=True), DEFAULT, now=NOW)
    assert "maintenance window" in why


def test_a_category_outside_the_list_is_not_ticketed():
    narrow = {**DEFAULT, "categories": ["power", "cooling"]}
    why = policy.explain(alarm(category="network"), narrow, now=NOW)
    assert "network" in why


def test_a_condition_younger_than_the_dwell_waits():
    fresh = alarm(first_seen=NOW - timedelta(seconds=30))
    why = policy.explain(fresh, DEFAULT, now=NOW)
    assert "dwell" in why and "30s" in why


def test_the_dwell_can_be_switched_off():
    fresh = alarm(first_seen=NOW - timedelta(seconds=1))
    assert policy.matches(fresh, {**DEFAULT, "dwell_s": 0}, now=NOW)


def test_an_unreadable_start_time_is_not_yet_rather_than_now():
    """Opening a ticket on a timestamp we could not read is the worse of the
    two mistakes: the next action on the same condition carries a usable one."""
    why = policy.explain(alarm(first_seen=None), DEFAULT, now=NOW)
    assert "start time" in why


def test_a_naive_timestamp_is_read_as_utc():
    """Rows from asyncpg are aware; a hand-built one in a fixture may not be,
    and subtracting a naive from an aware datetime raises rather than being
    quietly wrong."""
    naive = alarm(first_seen=datetime(2026, 9, 22, 7, 0))
    assert policy.matches(naive, DEFAULT, now=NOW)


def test_an_iso_string_is_accepted():
    """The outbox freezes its payload as JSONB, so a datetime comes back as a
    string. The policy is re-evaluated on that shape in the preview path."""
    row = alarm(first_seen="2026-09-22T07:00:00+00:00")
    assert policy.matches(row, DEFAULT, now=NOW)


# ------------------------------------------------------------- ordering

@pytest.mark.parametrize("floor,severity,expected", [
    ("CRITICAL", "CRITICAL", True),
    ("CRITICAL", "MAJOR", False),
    ("MINOR", "MAJOR", True),
    ("INFO", "INFO", True),
    ("INFO", "CLEAR", False),
])
def test_the_floor_is_at_least_not_exactly(floor, severity, expected):
    got = policy.matches(alarm(severity=severity),
                         {**DEFAULT, "min_severity": floor}, now=NOW)
    assert got is expected


def test_the_python_ranking_agrees_with_the_sql_one():
    """`SEVERITY_RANK` mirrors `_SEV_RANK` in the alarm repository, and the two
    are written out separately so that a comparison in a policy and a
    comparison in an ORDER BY cannot silently disagree about which alarm is
    worse. This is the assertion that keeps them honest.
    """
    sql = alarm_repo._SEV_RANK.format(col="x")
    for severity, rank in SEVERITY_RANK.items():
        if severity == "CLEAR":
            continue                        # the SQL falls through to ELSE 5
        assert f"WHEN '{severity}' THEN {rank}" in sql
