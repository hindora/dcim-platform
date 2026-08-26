"""Location decides which counter a condition lands in.

The estate's counters count the estate. The monitoring's own conditions -
ingest stalled, collector gone, worker heartbeat stale - belong to no site and
are carried by their own badge.

This is the arrangement every alarm standard arrives at for system
diagnostics. ISA-18.2 keeps them off the operator's alarm summary because the
operator can take no action about them, and because their presence ruins the
alarm-rate figure the standard is built around; the same split shows up as
"system alarms" in Niagara and Metasys, and as meta-monitoring routed to a
different receiver in every Prometheus deployment that has been paged at 3am.

Two properties have to hold, and they pull in opposite directions:

* the estate figure is the sum of the site rows, with no footnote - which is
  what broke before this pass, when the estate read 2 and both of its sites
  read 0;
* the pipeline's state is never silent. A stalled ingest worker that produces
  no visible change to a console is worse than a wrong number, because every
  green tile on the page becomes a claim nobody is checking.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.repositories import sites as sites_repo
from app.services import sites as sites_service


class _FakeSession:
    """The service never touches it: the repo calls are monkeypatched."""


def _returns(value):
    async def _call(*_a, **_kw):
        return value
    return _call


def _condition(alarm_type: str, severity: str, response_class: str,
               **extra: Any) -> dict[str, Any]:
    row = {
        "alarm_type": alarm_type, "instance": "worker-1", "severity": severity,
        "response_class": response_class, "message": f"{alarm_type} fired",
        "first_seen": "2026-08-26T21:14:00Z", "state": "ACTIVE",
    }
    row.update(extra)
    return row


async def _health(monkeypatch, conditions, age_s):
    monkeypatch.setattr(sites_service.repo, "platform_conditions",
                        _returns(conditions))
    monkeypatch.setattr(sites_service.repo, "telemetry_age_seconds",
                        _returns(age_s))
    return await sites_service._platform_health(_FakeSession())


# ------------------------------------------------------- the two populations


def test_the_strip_counts_only_what_the_table_can_place():
    """The SQL, not the wiring: a location join and a NOT NULL.

    Without both, the estate total includes rows no site row can, and the
    columns stop adding up in front of somebody who is trying to decide where
    to walk.
    """
    import inspect

    src = inspect.getsource(sites_repo.fleet_alert_totals)
    assert "JOIN dev ON dev.device_id = a.device_id" in src
    assert "dev.datacenter_id IS NOT NULL" in src


def test_the_badge_takes_exactly_what_the_strip_dropped():
    """Complementary by construction, so nothing falls between them.

    Same CTE, opposite side of the same test. If these two ever stop being
    each other's complement, a condition is either counted twice or by nobody -
    and the second is how a dead pipeline goes unnoticed.
    """
    import inspect

    src = inspect.getsource(sites_repo.platform_conditions)
    assert "dev.datacenter_id IS NULL" in src
    assert "a.state <> 'CLEARED' AND a.is_symptom = false" in src


# --------------------------------------------------------------- the verdict


@pytest.mark.asyncio
async def test_a_quiet_pipeline_with_fresh_data_reads_ok(monkeypatch):
    out = await _health(monkeypatch, [], 12.0)
    assert out["state"] == "ok"
    assert out["alarms"] == 0 and out["alerts"] == 0
    assert out["telemetry_stale"] is False


@pytest.mark.asyncio
async def test_an_informational_condition_degrades_but_does_not_alarm(monkeypatch):
    """Lag inside the warning band is worth showing and not worth waking for."""
    out = await _health(
        monkeypatch, [_condition("ingest_lag_high", "WARNING", "alert")], 30.0)
    assert out["state"] == "degraded"
    assert out["alerts"] == 1 and out["alarms"] == 0


@pytest.mark.asyncio
async def test_an_alarm_impairs(monkeypatch):
    out = await _health(
        monkeypatch, [_condition("collector_stale", "MAJOR", "alarm")], 40.0)
    assert out["state"] == "impaired"
    assert out["alarms"] == 1


@pytest.mark.asyncio
async def test_a_critical_condition_reads_blind(monkeypatch):
    out = await _health(
        monkeypatch, [_condition("ingest_stalled", "CRITICAL", "alarm")], 20.0)
    assert out["state"] == "blind"


@pytest.mark.asyncio
async def test_stale_telemetry_reads_blind_even_with_nothing_open(monkeypatch):
    """The failure the alarm list cannot describe.

    Data can stop arriving without any platform rule firing - a collector that
    is up, heartbeating and polling nothing still produces silence. The badge
    is driven by the age of the data as well as by the alarms, because the age
    is what actually invalidates the rest of the page.
    """
    out = await _health(monkeypatch, [], 900.0)
    assert out["state"] == "blind"
    assert out["telemetry_stale"] is True
    assert out["alarms"] == 0


@pytest.mark.asyncio
async def test_no_telemetry_at_all_is_not_treated_as_fresh(monkeypatch):
    """`None` is unknown, and unknown here is the worst case, not the best.

    An empty table read as age zero would paint the badge green on a platform
    that has never received a sample.
    """
    out = await _health(monkeypatch, [], None)
    assert out["state"] == "blind"
    assert out["telemetry_age_s"] is None
    assert out["telemetry_stale"] is True


@pytest.mark.asyncio
async def test_the_threshold_travels_with_the_number(monkeypatch):
    """A UI that hard-codes its own idea of stale will disagree with the badge."""
    out = await _health(monkeypatch, [], 10.0)
    assert out["telemetry_trusted_s"] == sites_service._TELEMETRY_TRUSTED_S


@pytest.mark.asyncio
async def test_the_conditions_are_carried_in_words(monkeypatch):
    """"2" does not tell an operator the tiles beside it are seven minutes old."""
    out = await _health(monkeypatch, [
        _condition("ingest_stalled", "CRITICAL", "alarm"),
        _condition("ingest_lag_high", "WARNING", "alert"),
    ], 480.0)
    assert [c["alarm_type"] for c in out["conditions"]] == [
        "ingest_stalled", "ingest_lag_high"]
    assert all(c["message"] and c["first_seen"] for c in out["conditions"])


# ------------------------------------------------------------- the home page


@pytest.mark.asyncio
async def test_the_overview_keeps_the_two_apart(monkeypatch):
    """The page gets both, and never one folded into the other."""
    monkeypatch.setattr(sites_service.repo, "site_rollups", _returns([]))
    monkeypatch.setattr(sites_service.repo, "room_rollups", _returns([]))
    monkeypatch.setattr(sites_service.repo, "fleet_alert_totals",
                        _returns({"total": 0}))
    monkeypatch.setattr(sites_service.repo, "platform_conditions",
                        _returns([_condition("ingest_stalled", "CRITICAL", "alarm")]))
    monkeypatch.setattr(sites_service.repo, "telemetry_age_seconds", _returns(9.0))

    out = await sites_service.overview(_FakeSession())

    assert out["totals"]["total"] == 0
    assert out["platform"]["state"] == "blind"
    assert out["platform"]["alarms"] == 1
    # The old key is gone: a page still reading it would silently show zero.
    assert "unlocated_alarms" not in out
