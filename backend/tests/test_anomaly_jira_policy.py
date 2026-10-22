"""A MINOR derived alert is ineligible for a ticket under the default policy.

The default policy wants response_class `alarm` and severity >= MAJOR
(integrations/config.py, integrations/policy.py). Derived alerts are capped at
MINOR, whose response class is `alert`, so none tickets by default; the only
ways in are naming the type in policy.alarm_types, a one-off ticket, or (after
the Phase 2 gate) L5 escalating a root to MAJOR.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.analytics import sweep as anomaly_sweep
from app.core import alert_taxonomy as tax
from app.integrations import config as icfg
from app.integrations import policy
from tests.anomaly_fakes import seed_rows

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
ALERTS = [r for r in seed_rows() if r["default_lane"] == "alert"]


def _alarm(row) -> dict:
    sev = anomaly_sweep.derived_severity(row["severity"])
    return {"alarm_type": row["alarm_type"], "severity": sev,
            "response_class": tax.response_class_for(sev),
            "category": tax.BY_ALARM_TYPE[row["alarm_type"]], "is_symptom": False,
            "shelved": False, "first_seen": NOW - timedelta(hours=1)}


@pytest.mark.parametrize("row", ALERTS, ids=[r["id"] for r in ALERTS])
def test_a_derived_alert_does_not_ticket_by_default(row):
    pol = icfg.resolved(None)["policy"]
    alarm = _alarm(row)
    assert alarm["response_class"] == "alert"
    assert not policy.matches(alarm, pol, now=NOW)
    assert "response class" in policy.explain(alarm, pol, now=NOW)


def test_opting_a_type_in_tickets_it_after_the_dwell():
    pol = {**icfg.resolved(None)["policy"], "alarm_types": ["crah_supply_over_setpoint"]}
    row = next(r for r in ALERTS if r["id"] == "CR-1")
    alarm = _alarm(row)
    assert policy.matches(alarm, pol, now=NOW)
    young = {**alarm, "first_seen": NOW - timedelta(seconds=60)}
    assert not policy.matches(young, pol, now=NOW)
    # A derived symptom under a root still does not ticket.
    assert not policy.matches({**alarm, "is_symptom": True}, pol, now=NOW)
