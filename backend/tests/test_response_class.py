"""Alarm or alert: the axis that says whether anybody must move now.

ISA-18.2 draws this line by REQUIRED RESPONSE, not by how bad a number looks -
an alarm demands operator action and expects an acknowledgement, an alert is
informational and belongs to whoever schedules the work. Three things have to
hold for that to survive contact with this codebase:

* It stays an attribute. The moment it becomes a category, a leak and a dirty
  filter on the same CDU stop being counted together as cooling.
* The default never files something as informational by accident. Silence is
  the one output an alarm system may not produce by mistake.
* A rule that disagrees with the severity default wins - that is what the
  override column is for, and until this pass the engine never read it.
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi import HTTPException

from app.alarms.engine import Candidate, Rule
from app.api.v1 import estate as estate_api
from app.core import alert_taxonomy as tax
from app.core.alert_taxonomy import CATEGORIES, RESPONSE_CLASSES
from app.repositories import sites as sites_repo
from app.services import sites as sites_service


class _FakeSession:
    """The services never touch it."""


# ------------------------------------------------------------ the two axes


def test_response_class_is_not_a_category():
    """Different question, different axis.

    "Who owns this" and "must somebody move now" are answered by different
    people, and a taxonomy that fuses them makes the plant team read two
    counters to see their own equipment.
    """
    assert not set(RESPONSE_CLASSES) & set(CATEGORIES)


def test_severity_decides_by_default():
    """Severity already encodes consequence here, so the axes agree.

    Phase 2 sorted the 36 equipment points that way on purpose: integrity
    faults that threaten load now are MAJOR, wear and hygiene are WARNING.
    """
    assert tax.response_class_for("CRITICAL") == tax.ALARM
    assert tax.response_class_for("MAJOR") == tax.ALARM
    assert tax.response_class_for("WARNING") == tax.ALERT
    assert tax.response_class_for("MINOR") == tax.ALERT
    assert tax.response_class_for("INFO") == tax.ALERT


def test_an_unknown_severity_reaches_the_console():
    """The unsafe default is `alert`, so it is not the default.

    A condition nobody classified should be argued about on the console, not
    filed quietly as something to look at next quarter.
    """
    assert tax.response_class_for(None) == tax.ALARM
    assert tax.response_class_for("MYSTERY") == tax.ALARM


def test_a_rule_may_disagree_with_its_severity():
    """Urgency and consequence usually agree. Usually is not always.

    A WARNING-severity condition on a single-corded load is still someone's
    phone call, and the rule is where that is said.
    """
    assert tax.response_class_for("WARNING", rule_class=tax.ALARM) == tax.ALARM
    assert tax.response_class_for("CRITICAL", rule_class=tax.ALERT) == tax.ALERT
    # Junk in the override is ignored rather than stored: it would otherwise
    # become a third class that no counter knows how to display.
    assert tax.response_class_for("MAJOR", rule_class="urgent-ish") == tax.ALARM


def test_the_sql_default_matches_the_python_one():
    """Backfill, insert and classifier must agree, or history disagrees with now."""
    case = tax.response_sql_case(severity_col="s")
    for severity, expected in tax.CLASS_BY_SEVERITY.items():
        assert f"WHEN s = '{severity}' THEN '{expected}'" in case
    assert f"ELSE '{tax.ALARM}'" in case

    with_rule = tax.response_sql_case(severity_col="s", rule_col=":rc")
    assert with_rule.startswith("COALESCE(:rc, CASE")


# ------------------------------------------------------- the engine carries it


def test_a_rule_override_reaches_the_alarm():
    """The column existed from phase 1 and nothing read it.

    `alarm_rule.category` shipped as an override that the engine never put on a
    candidate, so a rule could declare one and be ignored. Both overrides now
    travel; this is the test that says so.
    """
    rule = Rule(id="r1", name="n", alarm_type="t", severity="WARNING",
                message_tpl="m", category="capacity", response_class="alarm")
    c = Candidate(key=None, severity=rule.severity, message="m", source="threshold",
                  observed_at=None, rule_id=rule.id,
                  category=rule.category, response_class=rule.response_class)
    assert (c.category, c.response_class) == ("capacity", "alarm")


def test_a_rule_that_says_nothing_leaves_the_default_alone():
    """None, not a guess: the SQL default from severity has to be reachable."""
    rule = Rule(id="r1", name="n", alarm_type="t", severity="MAJOR", message_tpl="m")
    assert rule.response_class is None
    assert rule.category is None


# ------------------------------------------------------------- the roll-up


def test_the_home_rollup_counts_alarms_only():
    """The console filters on the class rather than reporting it.

    Every counter on that page - the eight categories, the severity columns,
    the drill-downs - reads this one CTE, so filtering here is what makes the
    whole screen mean "faults" rather than "everything open". An operator
    reading `0 cooling` is being told nothing needs a response, which is only
    true if the filter is in the population and not bolted on per counter.
    """
    assert f"a.response_class = '{tax.ALARM}'" in sites_repo._ALARM_CTE
    # And it is not silently reported as a facet as well: alerts are absent,
    # so a split of the population would read alarm=total, alert=0 and invite
    # the reader to think the estate has no informational conditions at all.
    assert "class_alert" not in sites_repo._AGG_COALESCE


def test_the_block_is_the_fault_count():
    """`total` is alarms, and the eight categories partition it."""
    block = sites_service._alarms({
        "alerts_total": 12, "crit": 3, "major": 9,
        "alerts_power": 5, "alerts_visibility": 7,
    })
    assert block["total"] == 12
    assert sum(block["by_category"].values()) == block["total"]
    assert "by_class" not in block


def test_a_quiet_site_reads_zero_not_missing():
    block = sites_service._alarms({"alerts_total": 0})
    assert block["total"] == 0
    assert set(block["by_category"]) == set(CATEGORIES)


# ------------------------------------------------------------------ the API


async def test_the_legend_defines_both_classes():
    """An operator who cannot define a facet will not use it."""
    legend = await estate_api.alarm_categories()
    assert [c["key"] for c in legend["response_classes"]] == list(RESPONSE_CLASSES)
    for c in legend["response_classes"]:
        assert c["label"] and c["description"]


@pytest.mark.parametrize("bad", ["urgent", "ALARM ", "event"])
async def test_an_unknown_response_class_is_rejected(bad):
    """A filter nobody validates returns everything and looks like a filter."""
    from app.api.v1 import alarms as alarms_api

    with pytest.raises(HTTPException) as exc:
        await alarms_api.list_alarms(state=None, severity=None, device_id=None,
                                     alarm_type=None, category=None,
                                     detection=None, response_class=[bad],
                                     include_symptoms=False, limit=10,
                                     session=_FakeSession())
    assert exc.value.status_code == 400


# ------------------------------------------------- the class follows severity


def test_an_update_reclassifies_but_does_not_recategorise():
    """Urgency is a property of the condition NOW; the category is history.

    A load alarm that escalates from WARNING to CRITICAL must stop being an
    alert on the same statement that raises its severity - otherwise the row
    reads CRITICAL on the console and is filed as informational underneath.
    The category is the opposite case and stays put: what kind of thing failed
    did not change, and rewriting it would move alarms between owners whenever
    a device was re-typed.
    """
    from app.repositories import alarms as repo

    sql = repo._ALARM_SELECT
    assert "a.response_class" in sql

    src = pathlib.Path(repo.__file__).read_text(encoding="utf-8")
    assert "response_class   = EXCLUDED.response_class" in src
    assert "category         = EXCLUDED.category" not in src
