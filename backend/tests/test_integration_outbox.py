"""Turning alarm actions into intents to ticket.

The rule this file exists to pin down: **the policy gates CREATION, not
follow-ups.** Once a ticket exists for a fingerprint, every later action on
that condition goes out whatever the policy has since been edited to say -
otherwise tightening the policy on a Tuesday afternoon strands every issue
opened that morning in the open state with nothing coming to close it.

The repository is monkeypatched rather than hit, because what is under test is
the decision, and the SQL has its own gate in the migrations job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.integrations import fingerprint as fp
from app.integrations import outbox

NOW = datetime.now(UTC)

INTEGRATION = {"id": "i1", "name": "Acme Jira", "config": {}}


@dataclass
class Action:
    kind: str
    alarm: dict


def wide(**over):
    base = {
        "id": "a1", "device_id": "d1", "alarm_type": "supply_temp_high",
        "instance": "AI:12", "severity": "MAJOR", "category": "cooling",
        "response_class": "alarm", "is_symptom": False, "shelved": False,
        "datacenter_code": "DC1", "device_name": "CRAH01",
        "first_seen": NOW - timedelta(hours=1),
    }
    base.update(over)
    return base


def thin(**over):
    base = {"id": "a1", "device_id": "d1", "alarm_type": "supply_temp_high",
            "instance": "AI:12", "severity": "MAJOR", "change": "created"}
    base.update(over)
    return base


@pytest.fixture
def fake(monkeypatch):
    """A recording stand-in for `app.repositories.integrations`."""

    class Fake:
        def __init__(self):
            self.integrations = [INTEGRATION]
            self.rows_by_id: dict[str, dict] = {}
            self.open: set[str] = set()
            self.written: list[dict] = []
            self.export_calls = 0

        async def active(self, session):
            return self.integrations

        async def alarms_for_export(self, session, ids):
            self.export_calls += 1
            return {i: self.rows_by_id[i] for i in ids if i in self.rows_by_id}

        async def open_fingerprints(self, session, integration_id, prints):
            return {p for p in prints if p in self.open}

        async def enqueue(self, session, rows):
            self.written.extend(rows)
            return len(rows)

    f = Fake()
    monkeypatch.setattr(outbox, "repo", f)
    return f


async def run(fake, actions, rows: list[dict[str, Any]]):
    fake.rows_by_id = {r["id"]: r for r in rows}
    return await outbox.enqueue_actions(None, actions)


# ------------------------------------------------------------- cheap path

async def test_nothing_is_read_when_there_are_no_actions(fake):
    assert await outbox.enqueue_actions(None, []) == 0
    assert fake.export_calls == 0


async def test_nothing_is_read_when_no_integration_is_enabled(fake):
    fake.integrations = []
    assert await outbox.enqueue_actions(None, [Action("alarm_created", thin())]) == 0
    assert fake.export_calls == 0


async def test_an_alarm_milder_than_any_policy_never_reaches_the_widening_query(fake):
    """The pre-filter runs on the thin dict the alarm layer already produced,
    so a healthy estate's ordinary INFO traffic costs no query at all."""
    await run(fake, [Action("alarm_created", thin(severity="CLEAR"))], [wide()])
    assert fake.export_calls == 0


# ------------------------------------------------------------ what counts

async def test_a_matching_raise_is_enqueued(fake):
    await run(fake, [Action("alarm_created", thin())], [wide()])
    assert len(fake.written) == 1
    row = fake.written[0]
    assert row["kind"] == "alarm_raised"
    assert row["fingerprint"] == fp.of_alarm(wide())
    assert row["integration_id"] == "i1"


async def test_the_payload_is_the_wide_row_not_the_thin_one(fake):
    """Frozen, and complete. The thin dict from raise_alarm's RETURNING has no
    location on it, and a ticket without a room is a ticket nobody can act on."""
    await run(fake, [Action("alarm_created", thin())], [wide()])
    payload = fake.written[0]["payload"]
    # None of these three exists on the thin dict `raise_alarm` returns.
    assert payload["device_name"] == "CRAH01"
    assert payload["category"] == "cooling"
    assert payload["datacenter_code"] == "DC1"


async def test_an_escalation_is_enqueued_but_a_mere_re_occurrence_is_not(fake):
    """A `touched` update is the same condition still being true, which is
    what an occurrence count is for. A write per poll per alarm is not."""
    fake.open.add(fp.of_alarm(wide()))
    await run(fake, [Action("alarm_updated", thin(change="escalated"))], [wide()])
    assert [r["kind"] for r in fake.written] == ["alarm_escalated"]

    fake.written.clear()
    await run(fake, [Action("alarm_updated", thin(change="touched"))], [wide()])
    assert fake.written == []


async def test_a_deescalation_is_not_worth_interrupting_a_service_desk(fake):
    fake.open.add(fp.of_alarm(wide()))
    await run(fake, [Action("alarm_updated", thin(change="deescalated"))], [wide()])
    assert fake.written == []


# --------------------------------------------------------------- refusals

async def test_a_symptom_is_not_enqueued(fake):
    await run(fake, [Action("alarm_created", thin())], [wide(is_symptom=True)])
    assert fake.written == []


async def test_a_shelved_alarm_is_not_enqueued(fake):
    await run(fake, [Action("alarm_created", thin())], [wide(shelved=True)])
    assert fake.written == []


async def test_an_alert_is_not_enqueued(fake):
    await run(fake, [Action("alarm_created", thin())],
              [wide(response_class="alert")])
    assert fake.written == []


async def test_a_clear_with_no_open_ticket_is_dropped(fake):
    """The normal case: most conditions never earned one."""
    await run(fake, [Action("alarm_cleared", thin())], [wide()])
    assert fake.written == []


# -------------------------------------------------------------- follow-ups

async def test_a_clear_goes_out_when_a_ticket_is_open(fake):
    fake.open.add(fp.of_alarm(wide()))
    await run(fake, [Action("alarm_cleared", thin())], [wide()])
    assert [r["kind"] for r in fake.written] == ["alarm_cleared"]


async def test_a_follow_up_ignores_a_policy_that_now_refuses_it(fake):
    """The rule this module exists for. The policy has been tightened since
    the ticket was opened; the clear must still reach it, or the issue sits
    open forever with nothing coming to close it."""
    fake.integrations = [{**INTEGRATION,
                          "config": {"policy": {"min_severity": "CRITICAL"}}}]
    fake.open.add(fp.of_alarm(wide()))
    await run(fake, [Action("alarm_cleared", thin())], [wide(severity="MAJOR")])
    assert [r["kind"] for r in fake.written] == ["alarm_cleared"]


async def test_without_an_open_ticket_the_tightened_policy_still_refuses(fake):
    fake.integrations = [{**INTEGRATION,
                          "config": {"policy": {"min_severity": "CRITICAL"}}}]
    await run(fake, [Action("alarm_created", thin())], [wide(severity="MAJOR")])
    assert fake.written == []


# ----------------------------------------------------------------- hygiene

async def test_one_row_per_fingerprint_and_kind_per_batch(fake):
    """A tick that raises the same condition twice must not pay for two Jira
    writes to say one thing."""
    await run(fake, [Action("alarm_created", thin()),
                     Action("alarm_created", thin())], [wide()])
    assert len(fake.written) == 1


async def test_an_alarm_that_vanished_between_the_action_and_the_query_is_skipped(fake):
    """A manual clear racing a sweep, or a device deleted mid-tick. Inventing
    a payload from the thin dict would put a ticket on the desk with no
    location on it."""
    await run(fake, [Action("alarm_created", thin())], [])
    assert fake.written == []


async def test_every_enabled_integration_gets_its_own_row(fake):
    """Each has its own dedup table, so one row cannot serve both."""
    fake.integrations = [INTEGRATION, {**INTEGRATION, "id": "i2",
                                       "name": "Second"}]
    await run(fake, [Action("alarm_created", thin())], [wide()])
    assert {r["integration_id"] for r in fake.written} == {"i1", "i2"}


async def test_the_widening_query_runs_once_for_a_whole_batch(fake):
    """One round trip per tick, not per alarm."""
    rows = [wide(id=f"a{n}", instance=f"AI:{n}") for n in range(5)]
    actions = [Action("alarm_created", thin(id=r["id"], instance=r["instance"]))
               for r in rows]
    await run(fake, actions, rows)
    assert fake.export_calls == 1
    assert len(fake.written) == 5
