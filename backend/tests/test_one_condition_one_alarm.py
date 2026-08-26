"""One condition is one alarm, however it was detected.

A pinned firewall CPU held TWO open alarms: `cpu_high_usage` raised by the
trap, `cpu_high` raised by the poll rule. One box, one CPU, two rows, two
names, two acknowledgements and two clears - and an operator who has to know
that the vendor's word and ours mean the same thing.

`detection` already records HOW a condition was noticed - threshold, state,
absence - and that is an attribute of the alarm. A second alarm type is not.

Two properties have to hold together:

* the trap and the rule file under the same name, so they meet on one row;
* meeting on one row must not let the milder detector demote the fiercer one.
  The trap fires CRITICAL the moment a CPU pins; the poll rule says WARNING two
  minutes later off its own lower threshold, and the naive upsert took the
  newest. The condition would be unchanged and the console would go quiet,
  which is the worst direction for an alarm system to be wrong in.
"""

from __future__ import annotations

import inspect
import re

from app.core.alert_taxonomy import CANONICAL_ALARM_TYPE, canonical_alarm_type
from app.repositories import alarms as repo

# ------------------------------------------------------------ the same name


def test_the_trap_and_the_rule_agree_on_a_name():
    assert canonical_alarm_type("cpu_high_usage") == "cpu_high"
    assert canonical_alarm_type("cpu_sustained") == "cpu_saturated"
    assert canonical_alarm_type("memory_high_usage") == "memory_high"


def test_a_name_with_no_alias_is_left_alone():
    """The map is for conditions that are genuinely the same one.

    `fan_failure` arrives by trap and by nothing else; `pdu_temp_high` is a PDU
    probe, not a chassis sensor. Aliasing those would merge conditions that are
    not the same - the opposite failure, and the worse one.
    """
    for name in ("fan_failure", "pdu_temp_high", "link_down", "ups_on_battery"):
        assert canonical_alarm_type(name) == name


def test_the_alias_target_is_never_itself_an_alias():
    """A chain would make the canonical name depend on lookup order."""
    for target in CANONICAL_ALARM_TYPE.values():
        assert target not in CANONICAL_ALARM_TYPE


def test_the_event_path_canonicalises_both_raise_and_clear():
    """A recovery trap speaks the trap's vocabulary.

    If only the raise is translated, the clear names a row that no longer
    exists and the alarm stays open while the device reports itself healthy.
    """
    from app.alarms import service

    src = inspect.getsource(service.AlarmService.handle_event)
    raise_site = src[src.index("raise_alarm"):]
    clear_site = src[:src.index("raise_alarm")]
    assert "canonical_alarm_type" in raise_site, "the raise is not canonical"
    assert "canonical_alarm_type" in clear_site, "the clear is not canonical"


# --------------------------------------------------- and one severity policy


def _upsert_sql() -> str:
    """The resolved statement, not the source line.

    The severity CASE is built from an f-string; a tool that strips the `f`
    prefix leaves the placeholders in the SQL and Postgres raises on a syntax
    error nobody reads. Asserting the resolved text is what catches that.
    """
    captured: dict[str, str] = {}

    class _Result:
        def mappings(self):
            return self

        def first(self):
            return None

    class _Session:
        async def execute(self, statement, params=None):
            captured["sql"] = str(statement)
            return _Result()

    import asyncio
    from datetime import UTC, datetime

    asyncio.run(repo.raise_alarm(
        _Session(), device_id="d", alarm_type="cpu_high", instance="",
        severity="MAJOR", message="m", source="snmp_trap",
        observed_at=datetime.now(UTC)))
    return captured["sql"]


def test_severity_escalates_and_only_its_own_source_may_lower_it():
    sql = _upsert_sql()
    case = sql[sql.index("severity         = CASE"):]
    case = case[:case.index("END,")]

    # Escalation: the incoming severity wins when it ranks worse (lower number).
    assert "EXCLUDED.severity" in case and "alarm.severity" in case
    assert "<" in case, "no rank comparison - severity is being taken blindly"
    # De-escalation: allowed for the source that set the current severity.
    assert "alarm.source IS NOT DISTINCT FROM EXCLUDED.source" in case


def test_the_rank_is_resolved_rather_than_left_as_a_placeholder():
    sql = _upsert_sql()
    assert "{_SEV_RANK" not in sql, "the f-string prefix was lost"
    assert re.search(r"CASE EXCLUDED\.severity::text WHEN 'CRITICAL' THEN 0", sql)
    assert re.search(r"CASE alarm\.severity::text WHEN 'CRITICAL' THEN 0", sql)


def test_every_severity_the_database_knows_is_ranked():
    """An unranked severity sorts as 5 and would never escalate anything."""
    sql = _upsert_sql()
    for name in ("CRITICAL", "MAJOR", "MINOR", "WARNING", "INFO"):
        assert f"WHEN '{name}'" in sql
