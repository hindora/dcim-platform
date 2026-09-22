"""The gate between a maintenance window and its change request.

Two properties, and both are about a window that must NOT open:

* a window requiring approval does not start until the change request says so,
  by the ticker or by the button;
* a DECLINED change cancels its window, because a window left scheduled would
  open at 02:00 and shelve alarms on equipment nobody is touching - which is
  the failure the gate exists to prevent, arrived at from the other direction.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations import change, inbox
from app.repositories import maintenance as maintenance_repo
from app.services import maintenance as service

# --------------------------------------------------- the gate is in the SQL

def test_the_ticker_query_refuses_an_unapproved_window():
    """The gate lives in the ticker's OWN query rather than as a check in the
    service, for the same reason `status` is a column and not a comparison
    against now(): one process decides, and a predicate in SQL cannot be
    forgotten by the next person who adds a transition."""
    import inspect
    sql = inspect.getsource(maintenance_repo.due_transitions)
    assert "NOT require_approval OR jira_approval_state = 'approved'" in sql


def test_a_window_that_never_opened_can_still_close():
    """Otherwise a declined change leaves a scheduled row in the table for
    ever, waiting for a start that will never come."""
    import inspect
    sql = inspect.getsource(maintenance_repo.due_transitions)
    assert "status = 'active' AND ends_at <= now()" in sql


def test_the_window_projection_says_why_it_is_held():
    """A window that silently fails to open reads as a DCIM fault. The
    engineer is already at the door."""
    import inspect
    sql = inspect.getsource(maintenance_repo)
    assert "blocked_reason" in sql
    assert "has not been approved" in sql


# ------------------------------------------------------------- the decision

@pytest.fixture
def fake(monkeypatch):
    class Repo:
        def __init__(self):
            self.window = {"id": "w1", "title": "Replace fan tray",
                           "status": "scheduled",
                           "jira_approval_state": "pending"}
            self.approval: str | None = None
            self.status: str | None = None

        async def set_approval(self, session, window_id, state):
            if self.window["jira_approval_state"] == state:
                return None
            self.approval = state
            return {"id": window_id, "title": self.window["title"],
                    "status": self.window["status"]}

        async def set_status(self, session, window_id, status):
            self.status = status

        async def get_window(self, session, window_id):
            return self.window

    repo = Repo()
    monkeypatch.setattr(service, "repo", repo)
    return repo


async def test_an_approval_unlocks_the_gate_but_does_not_start_the_window(fake):
    """"Approved" and "due" are different facts. A board approving at noon for
    a 02:00 window has not asked for it to begin now."""
    moved = await service.apply_approval(None, "w1", change.APPROVED)
    assert moved is not None
    assert fake.approval == "approved"
    assert fake.status is None


async def test_a_decline_cancels_the_window(fake):
    """The work is not happening. A window left scheduled would open at 02:00
    and shelve alarms on equipment nobody is touching."""
    moved = await service.apply_approval(None, "w1", change.DECLINED)
    assert fake.approval == "declined"
    assert fake.status == "cancelled"
    assert moved["cancelled"] is True


async def test_a_decline_after_the_window_started_does_not_cancel_it(fake):
    """Somebody is inside the equipment. Cancelling now would un-shelve every
    alarm mid-work and page the whole estate."""
    fake.window["status"] = "active"
    await service.apply_approval(None, "w1", change.DECLINED)
    assert fake.approval == "declined"
    assert fake.status is None


async def test_the_same_decision_twice_moves_nothing(fake):
    """A change request passes through half a dozen statuses and only two of
    them mean anything here, so most inbound events are correctly a no-op."""
    fake.window["jira_approval_state"] = "approved"
    assert await service.apply_approval(None, "w1", change.APPROVED) is None
    assert fake.status is None


# ------------------------------------------------------ inbound routing

@pytest.fixture
def inbound(monkeypatch):
    state: dict[str, Any] = {"window": None, "applied": [], "audited": []}

    class MaintRepo:
        async def window_by_issue(self, session, integration_id, issue_key):
            return state["window"]

    class Links:
        async def link_by_issue(self, session, integration_id, issue_key):
            return None

    class Audit:
        async def record(self, session, **kw):
            state["audited"].append(kw)

    class MaintService:
        async def apply_approval(self, session, window_id, verdict):
            state["applied"].append((window_id, verdict))
            return {"id": window_id, "cancelled": verdict == "declined"}

    monkeypatch.setattr(inbox, "maintenance_repo", MaintRepo())
    monkeypatch.setattr(inbox, "repo", Links())
    monkeypatch.setattr(inbox, "audit", Audit())
    import app.services.maintenance as real
    monkeypatch.setattr(real, "apply_approval", MaintService().apply_approval)
    return state


def moved_to(status: str, category: str = "indeterminate"):
    return {
        "webhookEvent": "jira:issue_updated",
        "user": {"accountId": "5b10a", "displayName": "Dana Okafor"},
        "issue": {"key": "DCOPS-900", "fields": {
            "status": {"name": status, "statusCategory": {"key": category}},
            "resolution": None}},
        "changelog": {"items": [{"field": "status", "fromString": "Draft",
                                 "toString": status}]},
    }


INTEGRATION = {"id": "i1", "name": "Acme Jira", "config": {}}


async def test_an_approval_on_a_change_request_reaches_its_window(inbound):
    inbound["window"] = {"id": "w1", "status": "scheduled",
                         "jira_approval_state": "pending"}
    result = await inbox.apply(None, INTEGRATION, moved_to("Approved"))
    assert result["action"] == "change_approved"
    assert inbound["applied"] == [("w1", "approved")]


async def test_a_decline_reaches_its_window_and_is_reported(inbound):
    inbound["window"] = {"id": "w1", "status": "scheduled",
                         "jira_approval_state": "pending"}
    result = await inbox.apply(None, INTEGRATION, moved_to("Declined"))
    assert result["action"] == "change_declined"
    assert result["cancelled"] is True


async def test_a_status_that_says_nothing_leaves_the_window_alone(inbound):
    inbound["window"] = {"id": "w1", "status": "scheduled",
                         "jira_approval_state": "pending"}
    result = await inbox.apply(None, INTEGRATION, moved_to("Awaiting CAB"))
    assert result["action"] == "change_noted"
    assert inbound["applied"] == []


async def test_a_decision_is_attributed_to_the_human_who_made_it(inbound):
    """"The window was cancelled" with no actor is the audit row that makes an
    incident review impossible."""
    inbound["window"] = {"id": "w1", "status": "scheduled",
                         "jira_approval_state": "pending"}
    await inbox.apply(None, INTEGRATION, moved_to("Declined"))
    row = inbound["audited"][-1]
    assert row["action"] == "maintenance.change.declined"
    assert "5b10a" in row["actor"]
    assert row["target_id"] == "w1"


async def test_an_issue_that_is_neither_a_condition_nor_a_window_is_unlinked(
        inbound):
    """The project is shared with humans. Normal, and counted rather than
    silently dropped, because a webhook filter that is too wide is otherwise
    invisible."""
    inbound["window"] = None
    result = await inbox.apply(None, INTEGRATION, moved_to("Approved"))
    assert result["action"] == "unlinked"


async def test_the_configured_status_lists_are_used(inbound):
    inbound["window"] = {"id": "w1", "status": "scheduled",
                         "jira_approval_state": "pending"}
    integration = {**INTEGRATION,
                   "config": {"change_approved_statuses": ["Go"]}}
    result = await inbox.apply(None, integration, moved_to("Go"))
    assert result["action"] == "change_approved"
