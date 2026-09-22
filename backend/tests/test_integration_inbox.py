"""Applying what Jira said, to the alarm it was about.

**The first test in this file is the one that matters.** A closed ticket is
not a cleared fault, and every other correct behaviour here is downstream of
that. An engineer who fixed the symptom, or who closed the ticket because the
part arrives Thursday, must not be able to silence a live fault from outside
the DCIM.

The repositories are monkeypatched rather than hit: what is under test is the
decision, and the SQL has its own gate in the migrations job.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations import inbox

INTEGRATION = {"id": "i1", "name": "Acme Jira", "config": {}}

OPEN_ALARM = {"id": "a1", "device_id": "d1", "alarm_type": "supply_temp_high",
              "instance": "AI:12", "severity": "MAJOR", "state": "ACTIVE"}


def link(**over):
    base = {"fingerprint": "abcd1234", "issue_key": "DCOPS-142",
            "alarm_id": "a1", "device_id": "d1",
            "alarm_type": "supply_temp_high", "instance": "AI:12",
            "closed_at": None, "status_category": None, "wont_reopen": False}
    base.update(over)
    return base


def updated(*, category="done", resolution="Done", to_status="Done"):
    return {
        "webhookEvent": "jira:issue_updated",
        "user": {"accountId": "5b10a", "displayName": "Sam Rivers"},
        "issue": {"key": "DCOPS-142", "fields": {
            "status": {"name": to_status, "statusCategory": {"key": category}},
            "resolution": {"name": resolution} if resolution else None}},
        "changelog": {"items": [{"field": "status", "fromString": "In Progress",
                                 "toString": to_status}]},
    }


@pytest.fixture
def fake(monkeypatch):
    class Links:
        def __init__(self):
            self.link: dict[str, Any] | None = link()
            self.recorded: list[dict] = []
            self.dropped: list[str] = []

        async def link_by_issue(self, session, integration_id, issue_key):
            return self.link

        async def record_inbound(self, session, fingerprint, **kw):
            self.recorded.append({"fingerprint": fingerprint, **kw})

        async def drop_link(self, session, fingerprint):
            self.dropped.append(fingerprint)
            return True

    class Alarms:
        def __init__(self):
            self.open: dict[str, Any] | None = dict(OPEN_ALARM)
            self.acknowledged: list[tuple] = []
            self.cleared: list[str] = []
            self.history: list[dict] = []
            self.ack_returns_none = False

        async def open_alarm_for(self, session, *, device_id, alarm_type,
                                 instance):
            return self.open

        async def get_alarm(self, session, alarm_id):
            return self.open

        async def acknowledge(self, session, alarm_id, actor, note):
            if self.ack_returns_none:
                return None
            self.acknowledged.append((alarm_id, actor, note))
            return {"id": alarm_id, "device_id": "d1", "severity": "MAJOR"}

        async def manual_clear(self, session, alarm_id, actor):
            self.cleared.append(alarm_id)
            return {"id": alarm_id, "device_id": "d1", "severity": "MAJOR",
                    "instance": "AI:12"}

        async def record_history(self, session, **kw):
            self.history.append(kw)

        async def refresh_device_alarm_state(self, session, ids):
            return None

    links, alarms = Links(), Alarms()
    audited: list[dict] = []

    class Audit:
        async def record(self, session, **kw):
            audited.append(kw)

    class Correlation:
        async def release_symptoms(self, session, root_id):
            return []

    class MaintRepo:
        """No window owns any of these issues.

        Needed since phase 4: an event whose issue key matches no CONDITION
        falls through to "is this a maintenance window's change request", and
        an unfaked repository there would reach a real database.
        """

        async def window_by_issue(self, session, integration_id, issue_key):
            return None

    class LinkCorrelation:
        async def refresh_link_state(self, session, **kw):
            return None

    monkeypatch.setattr(inbox, "repo", links)
    monkeypatch.setattr(inbox, "alarm_repo", alarms)
    monkeypatch.setattr(inbox, "audit", Audit())
    monkeypatch.setattr(inbox, "correlation", Correlation())
    monkeypatch.setattr(inbox, "link_correlation", LinkCorrelation())
    monkeypatch.setattr(inbox, "maintenance_repo", MaintRepo())
    links.alarms, links.audited = alarms, audited
    return links


# ------------------------------------------------------------- THE rule

async def test_done_acknowledges_and_never_clears(fake):
    """The rule the whole inbound design rests on.

    Only the poll clears. If the condition really is over, the next poll
    clears it within one interval and the outbound side comments on the ticket
    saying so - and if it is not over, the alarm is still on the console where
    somebody can see it.
    """
    result = await inbox.apply(None, INTEGRATION, updated())
    assert result["action"] == "acknowledge"
    assert fake.alarms.acknowledged and not fake.alarms.cleared


def test_the_default_configuration_does_not_trust_jira_over_the_plane():
    from app.integrations.config import resolved
    assert resolved({})["allow_clear_on_transition"] is False


async def test_the_opt_in_exception_clears_and_is_loud(fake):
    """For alarm types with no polled backstop. Off by default, audited as
    having happened `via` the setting, so the choice is visible afterwards."""
    integration = {**INTEGRATION,
                   "config": {"allow_clear_on_transition": True}}
    result = await inbox.apply(None, integration, updated())
    assert result["action"] == "clear"
    assert fake.alarms.cleared == ["a1"]
    assert fake.audited[-1]["after"]["via"] == "allow_clear_on_transition"


async def test_the_acknowledgement_names_the_ticket_and_the_human(fake):
    await inbox.apply(None, INTEGRATION, updated())
    alarm_id, actor, note = fake.alarms.acknowledged[0]
    assert alarm_id == "a1"
    assert "5b10a" in actor
    assert "DCOPS-142" in note


async def test_an_acknowledgement_writes_history_and_an_audit_row(fake):
    await inbox.apply(None, INTEGRATION, updated())
    assert fake.alarms.history[-1]["action"] == "acknowledged"
    assert fake.audited[-1]["action"] == "integration.inbound.acknowledge"
    assert fake.audited[-1]["target_id"] == "abcd1234"


# ------------------------------------------------------ which alarm

async def test_the_alarm_is_resolved_by_condition_not_by_the_stored_row(fake):
    """A condition clears at 02:00 and raises again at 06:00 as a new row
    with a new uuid. An engineer closing the ticket at 09:00 means the one
    that is open NOW."""
    fake.alarms.open = {**OPEN_ALARM, "id": "a2"}
    result = await inbox.apply(None, INTEGRATION, updated())
    assert result["alarm"] == "a2"


async def test_closing_a_ticket_for_a_fault_that_already_cleared_is_fine(fake):
    """The happy path: the fault went away and somebody tidied up after it."""
    fake.alarms.open = None
    fake.link = link(alarm_id=None)
    result = await inbox.apply(None, INTEGRATION, updated())
    assert result["action"] == "acknowledge" and result["alarm"] is None
    assert not fake.alarms.acknowledged


async def test_an_alarm_that_is_already_acknowledged_is_not_an_error(fake):
    """Two people reaching for one alarm is normal. Nothing moved, and the
    audit row still records that Jira asked."""
    fake.alarms.ack_returns_none = True
    result = await inbox.apply(None, INTEGRATION, updated())
    assert result["changed"] is False
    assert fake.audited[-1]["after"]["outcome"] == "alarm was not ACTIVE"


# --------------------------------------------------------------- declined

async def test_a_declined_ticket_closes_the_link_and_leaves_the_alarm(fake):
    """Won't Fix is a decision about the TICKET. Nobody said the condition was
    dealt with, so acknowledging would take a live fault off the console."""
    result = await inbox.apply(None, INTEGRATION,
                               updated(resolution="Won't Fix"))
    assert result["action"] == "declined"
    assert not fake.alarms.acknowledged and not fake.alarms.cleared
    assert fake.recorded[-1]["wont_reopen"] is True
    assert fake.recorded[-1]["closed"] is True


async def test_the_declined_list_comes_from_the_integrations_configuration(fake):
    integration = {**INTEGRATION,
                   "config": {"wont_reopen_resolutions": ["Not planned"]}}
    result = await inbox.apply(None, integration,
                               updated(resolution="Not planned"))
    assert result["action"] == "declined"


async def test_a_resolution_not_on_the_list_still_acknowledges(fake):
    integration = {**INTEGRATION,
                   "config": {"wont_reopen_resolutions": ["Not planned"]}}
    result = await inbox.apply(None, integration,
                               updated(resolution="Won't Fix"))
    assert result["action"] == "acknowledge"


# ---------------------------------------------------------- reopen/progress

async def test_a_move_out_of_done_on_a_closed_link_is_a_reopen(fake):
    fake.link = link(closed_at="2026-09-21T09:00:00+00:00")
    result = await inbox.apply(None, INTEGRATION,
                               updated(category="indeterminate",
                                       resolution=None,
                                       to_status="In Progress"))
    assert result["action"] == "reopened"
    assert fake.recorded[-1]["reopened"] is True
    assert fake.alarms.history[-1]["action"] == "jira_reopened"


async def test_a_move_between_two_open_statuses_is_only_progress(fake):
    """Somebody picked the ticket up. An alarm is acknowledged by a decision,
    not by somebody dragging a card."""
    result = await inbox.apply(None, INTEGRATION,
                               updated(category="indeterminate",
                                       resolution=None,
                                       to_status="In Progress"))
    assert result["action"] == "progress"
    assert not fake.alarms.acknowledged
    assert fake.alarms.history[-1]["action"] == "jira_in_progress"


# --------------------------------------------------------------- the rest

async def test_a_comment_lands_on_the_alarms_history(fake):
    """Without this, what an engineer found lives only in Jira, and the
    DCIM's history of a recurring fault is a list of raises with no
    explanation attached to any of them."""
    result = await inbox.apply(None, INTEGRATION, {
        "webhookEvent": "comment_created",
        "issue": {"key": "DCOPS-142"},
        "comment": {"body": "Filter was blocked."}})
    assert result["action"] == "comment"
    assert fake.alarms.history[-1]["action"] == "jira_comment"
    assert fake.alarms.history[-1]["detail"]["note"] == "Filter was blocked."


async def test_a_deleted_issue_drops_the_link_and_is_audited(fake):
    """So the next occurrence opens a new ticket instead of commenting into a
    void and reporting success."""
    result = await inbox.apply(None, INTEGRATION, {
        "webhookEvent": "jira:issue_deleted",
        "issue": {"key": "DCOPS-142"}})
    assert result["action"] == "orphaned"
    assert fake.dropped == ["abcd1234"]
    assert fake.audited[-1]["action"] == "integration.inbound.orphaned"


async def test_an_event_for_an_issue_we_did_not_open_is_counted_not_acted_on(fake):
    """The project is shared with humans. Normal, and deliberately not an
    error - but counted, because a webhook filter that is too wide is
    otherwise invisible."""
    fake.link = None
    result = await inbox.apply(None, INTEGRATION, updated())
    assert result["action"] == "unlinked"
    assert not fake.alarms.acknowledged


async def test_an_ignorable_event_touches_nothing(fake):
    payload = updated()
    payload["changelog"]["items"] = [{"field": "description",
                                      "fromString": "a", "toString": "b"}]
    result = await inbox.apply(None, INTEGRATION, payload)
    assert result == {"action": "ignore"}
    assert not fake.recorded and not fake.alarms.acknowledged
