"""The catalogue behind the Alarm status window.

It exists because a legend that defines eight buckets cannot answer the
question an operator actually arrives with - "what can this thing raise, and
will it ring at 3am". The properties below are the ones that make the answer
trustworthy rather than decorative:

* It is assembled from the rules table, not transcribed beside it. A rule
  somebody disables has to show as disabled on the next page load.
* It never states a severity it does not know. A trap arrives with the severity
  the device chose, so promising a class for it would be a promise this
  platform cannot keep.
* Reserved names say they are reserved. A capacity counter reading zero because
  nothing watches it is a different fact from one reading zero because the
  estate is well, and the legend is where that difference gets said.
"""

from __future__ import annotations

import pytest

from app.core import alert_taxonomy as tax
from app.services import taxonomy


class _FakeSession:
    """Never touched: the repository call is stubbed."""


def _rule(alarm_type, severity, **kw):
    row = {
        "alarm_type": alarm_type, "severity": severity, "metric_key": None,
        "operator": None, "threshold": None, "enabled": True,
        "instances": [], "response_class": None,
    }
    row.update(kw)
    return row


@pytest.fixture
def rules(monkeypatch):
    """Stand in for `alarm_rule`, so the catalogue is tested and not the seed."""
    seeded: list[dict] = []

    async def _list_rules(_session):
        return seeded

    monkeypatch.setattr(taxonomy.repo, "list_rules", _list_rules)
    return seeded


async def _build(rules_fixture) -> dict:
    return await taxonomy.catalogue(_FakeSession())


def _find(out, key):
    return next(c for c in out["conditions"] if c["key"] == key)


async def test_a_rule_carries_its_severity_class_and_threshold(rules):
    rules.append(_rule("inlet_temp_high", "WARNING", metric_key="inlet_temperature",
                       operator=">", threshold=27.0))
    out = await _build(rules)

    c = _find(out, "inlet_temp_high")
    assert c["origin"] == taxonomy.RULE
    assert c["severity"] == "WARNING"
    assert c["response_class"] == tax.ALERT
    assert c["detail"] == "inlet_temperature > 27"


async def test_a_disabled_rule_still_appears_and_says_so(rules):
    """Hiding it would read as "this platform cannot detect that".

    It can; somebody turned it off, and that is a different sentence - one an
    operator wondering why nothing fired needs to be able to find.
    """
    rules.append(_rule("power_draw_high", "MAJOR", metric_key="power_draw",
                       enabled=False))
    out = await _build(rules)

    c = _find(out, "power_draw_high")
    assert c["enabled"] is False
    assert out["summary"]["disabled"] == 1


async def test_a_role_sensitive_rule_has_no_fixed_category(rules):
    """`power_draw` is the electrical chain on a PDU and the host on a server.

    The classifier resolves that per alarm, from the device. The catalogue says
    null rather than `uncategorised`, which would claim the classifier has a
    gap where it actually has a rule.
    """
    rules.append(_rule("power_draw_high", "MAJOR", metric_key="power_draw"))
    out = await _build(rules)
    assert _find(out, "power_draw_high")["category"] is None


async def test_equipment_points_are_listed_one_by_one(rules):
    """Thirty-six points share one rule and one metric.

    An operator looking for "Alarm_Leak" will not find it under a rule called
    equipment-alarm-major, so the points are what the catalogue lists.
    """
    rules.append(_rule("equipment_alarm", "MAJOR", metric_key="alarm_state",
                       instances=["Alarm_Leak", "Battery_Fault"]))
    rules.append(_rule("equipment_alarm", "WARNING", metric_key="alarm_state",
                       instances=["Filter_Dirty"]))
    out = await _build(rules)

    leak = _find(out, "Alarm_Leak")
    assert leak["origin"] == taxonomy.EQUIPMENT
    assert leak["response_class"] == tax.ALARM
    assert leak["label"] == "Leak"          # the Alarm_ prefix is on all of them
    assert _find(out, "Filter_Dirty")["response_class"] == tax.ALERT
    assert _find(out, "Battery_Fault")["label"] == "Battery fault"


async def test_the_platform_conditions_do_not_claim_a_severity(rules):
    """`ingest_lag_high` is a WARNING until the pipeline stops, then CRITICAL.

    One severity here would be wrong half the time, and the class follows
    severity - so both stay null and the row says the class follows.
    """
    out = await _build(rules)

    c = _find(out, "ingest_lag_high")
    assert c["origin"] == taxonomy.PLATFORM
    assert c["severity"] is None and c["response_class"] is None


async def test_trap_conditions_follow_the_device(rules):
    out = await _build(rules)
    c = _find(out, "ups_on_battery")
    assert c["origin"] == taxonomy.REPORTED
    assert c["category"] == "power"
    assert c["response_class"] is None


async def test_reserved_names_are_marked_not_built(rules):
    out = await _build(rules)
    for key in tax.RESERVED:
        c = _find(out, key)
        assert c["origin"] == taxonomy.PLANNED
        assert c["enabled"] is False
    assert out["summary"]["planned"] == len(tax.RESERVED)
    # And they are not counted as switched-off rules: nobody switched them off.
    assert out["summary"]["disabled"] == 0


async def test_every_classified_condition_is_listed_once(rules):
    """The catalogue is the classifier's list, not a subset of it.

    A condition the classifier knows and the legend omits is exactly the gap
    that makes an operator stop trusting the legend.
    """
    rules.append(_rule("cpu_high", "WARNING", metric_key="cpu_utilization"))
    out = await _build(rules)

    keys = [c["key"] for c in out["conditions"]]
    assert len(keys) == len(set(keys)), "a condition is listed twice"
    for alarm_type in tax.BY_ALARM_TYPE:
        assert alarm_type in keys


async def test_the_summary_adds_up(rules):
    rules.append(_rule("cpu_temp_critical", "CRITICAL", metric_key="cpu_temperature"))
    rules.append(_rule("humidity_high", "WARNING", metric_key="relative_humidity"))
    out = await _build(rules)

    s = out["summary"]
    assert s["alarm"] + s["alert"] + s["varies"] == s["total"]
