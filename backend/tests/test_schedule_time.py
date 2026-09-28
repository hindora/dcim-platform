"""When a discovery schedule runs next, and when a range's audit has gone stale.

The timing is a local fact - 02:00 in Mumbai is not 02:00 UTC - so the rules are
tested in real zones, across a weekend and a DST change, with no clock involved.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from pathlib import Path

import pytest

from app.services import discovery_ranges as dr
from app.services import schedule_time as st

INTERVALS = (6, 12, 24, 48, 168)
APP = Path(__file__).resolve().parents[1] / "app"
MIG = next((APP.parent / "alembic" / "versions").glob("0083_*.py")).read_text(
    encoding="utf-8")


def utc(*a):
    return datetime(*a, tzinfo=UTC)


# ------------------------------------------------------------ next occurrence

def test_daily_at_two_is_two_local_not_two_utc():
    # 2026-09-28 is a Monday. 02:00 in Kolkata is 20:30 UTC the evening before.
    nxt = st.next_occurrence(time(2, 0), list(st.ALL_DAYS), "Asia/Kolkata",
                             utc(2026, 9, 28, 12, 0))
    assert nxt == utc(2026, 9, 28, 20, 30)


def test_strictly_after_so_a_run_at_its_own_slot_moves_to_the_next():
    at = utc(2026, 9, 29, 2, 0)
    assert st.next_occurrence(time(2, 0), list(st.ALL_DAYS), "UTC", at) \
        == utc(2026, 9, 30, 2, 0)


def test_weekdays_skip_the_weekend():
    friday_after = utc(2026, 10, 2, 3, 0)            # Friday 03:00 UTC
    assert st.next_occurrence(time(2, 0), [1, 2, 3, 4, 5], "UTC", friday_after) \
        == utc(2026, 10, 5, 2, 0)                     # Monday


def test_it_stays_local_across_a_dst_change():
    """London goes to GMT on 2026-10-25: 02:00 local is 01:00 UTC before and
    02:00 UTC after. A schedule computed in UTC would drift an hour."""
    before = st.next_occurrence(time(2, 0), list(st.ALL_DAYS), "Europe/London",
                                utc(2026, 10, 23, 12, 0))
    after = st.next_occurrence(time(2, 0), list(st.ALL_DAYS), "Europe/London",
                               utc(2026, 10, 26, 12, 0))
    assert before == utc(2026, 10, 24, 1, 0)
    assert after == utc(2026, 10, 27, 2, 0)


@pytest.mark.parametrize(("days", "hours"), [
    ([1, 2, 3, 4, 5, 6, 7], 24), ([1, 2, 3, 4, 5], 72), ([1], 168), ([1, 4], 96),
])
def test_the_longest_gap_is_what_on_time_means(days, hours):
    assert st.longest_gap_hours(days) == hours


# ------------------------------------------------------------------ timing

def test_a_timed_schedule_stores_its_gap_as_its_interval():
    t = st.timing("02:00", [1, 2, 3, 4, 5], "Asia/Kolkata", None, INTERVALS)
    assert t == {"run_at": "02:00", "days": [1, 2, 3, 4, 5],
                 "timezone": "Asia/Kolkata", "interval_hours": 72}


def test_an_interval_schedule_clears_the_time_fields():
    assert st.timing(None, [1], "UTC", 12, INTERVALS) == {
        "run_at": None, "days": None, "timezone": None, "interval_hours": 12}


@pytest.mark.parametrize(("run_at", "days", "tz", "hours"), [
    ("25:00", None, "UTC", None), ("2am", None, "UTC", None),
    ("02:00", [0], "UTC", None), ("02:00", [8], "UTC", None),
    ("02:00", None, "Mars/Olympus", None), (None, None, None, 5),
])
def test_what_cannot_be_scheduled_is_refused(run_at, days, tz, hours):
    with pytest.raises(st.ScheduleTimeError):
        st.timing(run_at, days, tz, hours, INTERVALS)


def test_the_migration_holds_a_timed_schedule_to_a_zone_and_days():
    assert "run_at IS NULL OR (timezone IS NOT NULL AND cardinality(days) >= 1" in MIG


# ----------------------------------------------------------- audit freshness

NOW = utc(2026, 9, 28, 12, 0).timestamp()


def _range(**kw):
    return {"enabled": True, "schedule_hours": 24,
            "last_full_at": utc(2026, 9, 28, 2, 0), **kw}


def test_a_range_swept_on_time_is_ok():
    assert dr.audit_state(_range(), NOW) == "ok"


def test_twice_its_gap_late_is_overdue():
    """Once late is a slow collector or a run that waited its turn; twice is an
    audit that has stopped happening."""
    assert dr.audit_state(_range(last_full_at=utc(2026, 9, 27, 0, 0)), NOW) == "ok"
    assert dr.audit_state(_range(last_full_at=utc(2026, 9, 26, 11, 0)), NOW) == "overdue"


def test_never_unscheduled_and_off_are_their_own_states():
    assert dr.audit_state(_range(last_full_at=None), NOW) == "never"
    assert dr.audit_state(_range(schedule_hours=None), NOW) == "unscheduled"
    assert dr.audit_state(_range(enabled=False), NOW) == "off"


def test_only_a_sweep_of_the_whole_range_counts_as_fresh():
    src = (APP / "repositories" / "discovery_ranges.py").read_text(encoding="utf-8")
    assert "r.cidr <<= CAST(s.c AS inet)" in src
    assert "sch.enabled AND r.id = ANY(sch.range_ids)" in src


def test_a_retimed_schedule_moves_its_next_run():
    """Changed from daily to weekdays at 02:00, it must not fire once more at its
    old time first."""
    svc = (APP / "services" / "discovery.py").read_text(encoding="utf-8")
    body = svc[svc.index("async def update_schedule("):svc.index("async def fire_due_schedule(")]
    assert 'fields.setdefault("next_run_at"' in body
    # Mondays at 02:00, re-timed on a Monday afternoon: next Monday.
    assert st.next_after({"run_at": "02:00", "days": [1], "timezone": "UTC"},
                         utc(2026, 9, 28, 12, 0)) == utc(2026, 10, 5, 2, 0)


def test_a_time_travels_to_the_database_as_text():
    """Found live: asyncpg types `CAST(:run_at AS time)` as a time parameter and
    refuses the string "02:00" - a 500 on every timed schedule. Cast via text."""
    src = (APP / "repositories" / "discovery.py").read_text(encoding="utf-8")
    assert "CAST(:run_at AS time)" not in src
    assert src.count("CAST(CAST(:run_at AS text) AS time)") == 2
    # The same for Run now and re-timing, which send the next run as ISO text.
    assert "CAST(CAST(:next_run_at AS text) AS timestamptz)" in src


def test_a_legacy_zone_name_a_browser_reports_is_accepted():
    """Found live: Chrome reports Asia/Calcutta, Ubuntu 24.04 ships it only in
    tzdata-legacy, and the schedule was refused. The tzdata package is declared so
    zoneinfo can always fall back to the full database."""
    pyproject = (APP.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert '"tzdata>=' in pyproject
    t = st.timing("02:00", None, "Asia/Calcutta", None, INTERVALS)
    assert t["timezone"] == "Asia/Calcutta"
