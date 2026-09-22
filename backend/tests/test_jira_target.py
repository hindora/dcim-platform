"""The issue lifecycle: what one outbox row does to one ticket.

These are the tests that protect the difference between a usable integration
and LibreNMS's - which posts `/rest/api/latest/issue` and keeps no memory of
the key, so every firing of the same alert creates another issue.

Driven through a recording double rather than httpx, because what is under
test here is the DECISION TREE, not the wire format; the wire format has its
own file.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.integrations import fingerprint as fp
from app.integrations.config import resolved
from app.integrations.jira.client import JiraError
from app.integrations.jira.target import API, IssueTarget

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)

ALARM = {
    "id": "0f2b7c1e-0000-4000-8000-000000000009",
    "device_id": "9f1d8a2e-0000-4000-8000-000000000001",
    "device_name": "CRAH01-DC1-HA", "device_type": "crah",
    "alarm_type": "supply_temp_high", "instance": "AI:12",
    "severity": "MAJOR", "message": "Supply air 28.4C above 26.0C",
    "metric_key": "supply_temp_c", "trigger_value": 28.4, "threshold": 26.0,
    "category": "cooling", "detection": "threshold", "source": "bacnet",
    "datacenter_code": "DC1", "room_name": "Server Hall A", "rack_name": None,
    "u_start": None, "mgmt_ip": "10.52.11.9", "serial_number": None,
    "asset_tag": None, "first_seen": NOW - timedelta(hours=1),
    "last_seen": NOW, "occurrence_count": 4,
}
PRINT = fp.of_alarm(ALARM)


class Recorder:
    """A JiraClient-shaped double that records calls and replays canned answers.

    ``routes`` maps "METHOD /path" to a response or a callable. Anything not
    routed gets ``{}``, which is what most calls here legitimately return.
    """

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    async def request(self, method, path, *, json_body=None, params=None,
                      issue_key=None, files=None):
        self.calls.append((method, path, json_body))
        handler = self.routes.get(f"{method} {path}")
        if callable(handler):
            return handler(json_body)
        if isinstance(handler, Exception):
            raise handler
        return handler if handler is not None else {}

    async def get(self, path, **kw):
        return await self.request("GET", path, **kw)

    async def post(self, path, **kw):
        return await self.request("POST", path, **kw)

    async def put(self, path, **kw):
        return await self.request("PUT", path, **kw)

    def paths(self, method=None):
        return [p for m, p, _ in self.calls if method is None or m == method]

    def body(self, method, path):
        for m, p, b in self.calls:
            if m == method and p == path:
                return b
        raise AssertionError(f"no {method} {path} in {self.paths()}")


def target_for(recorder, **config):
    cfg = resolved({"project_key": "DCOPS", "issue_type": "Incident", **config})
    return IssueTarget(recorder, cfg, base_url="https://acme.atlassian.net",
                       dcim_base="https://dcim.example.com")


CREATED = {"key": "DCOPS-142", "id": "10142"}


# ----------------------------------------------------------------- create

async def test_a_first_raise_creates_and_reports_the_key():
    rec = Recorder({f"POST {API}/issue": CREATED})
    update = await target_for(rec).handle(
        kind="alarm_raised", alarm=ALARM, print_=PRINT, link=None, now=NOW)
    assert update.issue_key == "DCOPS-142"
    assert update.issue_id == "10142"


async def test_the_create_carries_the_project_type_priority_and_fingerprint():
    rec = Recorder({f"POST {API}/issue": CREATED})
    await target_for(rec).handle(kind="alarm_raised", alarm=ALARM,
                                 print_=PRINT, link=None, now=NOW)
    fields = rec.body("POST", f"{API}/issue")["fields"]
    assert fields["project"] == {"key": "DCOPS"}
    assert fields["issuetype"] == {"name": "Incident"}
    assert fields["priority"] == {"name": "High"}
    assert fp.label(PRINT) in fields["labels"]
    assert fields["description"]["type"] == "doc"


async def test_the_create_carries_the_fingerprint_as_an_issue_property():
    """Structured and invisible in the UI, where the label is fast in JQL.
    Both, because they answer different questions."""
    rec = Recorder({f"POST {API}/issue": CREATED})
    await target_for(rec).handle(kind="alarm_raised", alarm=ALARM,
                                 print_=PRINT, link=None, now=NOW)
    prop = rec.body("POST", f"{API}/issue")["properties"][0]
    assert prop["key"] == "dcim.alarm"
    assert prop["value"]["fingerprint"] == PRINT
    assert prop["value"]["instance"] == "AI:12"


async def test_the_back_link_is_posted_after_the_create():
    rec = Recorder({f"POST {API}/issue": CREATED})
    await target_for(rec).handle(kind="alarm_raised", alarm=ALARM,
                                 print_=PRINT, link=None, now=NOW)
    body = rec.body("POST", f"{API}/issue/DCOPS-142/remotelink")
    assert body["globalId"].endswith(f"&alarm={PRINT}")
    assert body["object"]["status"] == {"resolved": False}


async def test_a_failed_back_link_does_not_fail_the_row():
    """The ticket exists and carries the whole story. Failing here would
    retry the CREATE and make a second ticket - far worse than no link."""
    rec = Recorder({f"POST {API}/issue": CREATED,
                    f"POST {API}/issue/DCOPS-142/remotelink":
                        JiraError("nope", status=400)})
    update = await target_for(rec).handle(
        kind="alarm_raised", alarm=ALARM, print_=PRINT, link=None, now=NOW)
    assert update.issue_key == "DCOPS-142"


async def test_a_clear_with_no_ticket_does_nothing():
    """The normal case on a healthy estate: most conditions never earned one."""
    rec = Recorder()
    assert await target_for(rec).handle(
        kind="alarm_cleared", alarm=ALARM, print_=PRINT, link=None,
        now=NOW) is None
    assert rec.calls == []


# ------------------------------------------------------------- open ticket

def open_link(**over):
    base = {"issue_key": "DCOPS-142", "issue_id": "10142", "closed_at": None,
            "wont_reopen": False, "last_pushed_at": None}
    base.update(over)
    return base


async def test_a_re_raise_on_an_open_ticket_does_not_create_a_second():
    """The property LibreNMS's transport lacks."""
    rec = Recorder()
    await target_for(rec, fields={"occurrences": "customfield_10104"}).handle(
        kind="alarm_raised", alarm=ALARM, print_=PRINT, link=open_link(),
        now=NOW)
    assert f"{API}/issue" not in rec.paths("POST")


async def test_a_re_raise_updates_a_field_rather_than_adding_a_comment():
    """One write, not two, and no noise. Jira limits writes against a single
    issue to 20 in two seconds and a flapping condition aims the whole
    estate's traffic at one key."""
    rec = Recorder()
    await target_for(rec, fields={"occurrences": "customfield_10104"}).handle(
        kind="alarm_raised", alarm=ALARM, print_=PRINT, link=open_link(),
        now=NOW)
    assert rec.body("PUT", f"{API}/issue/DCOPS-142") == {
        "fields": {"customfield_10104": 4}}
    assert f"{API}/issue/DCOPS-142/comment" not in rec.paths("POST")


async def test_an_escalation_raises_the_priority_and_says_so():
    rec = Recorder()
    hot = {**ALARM, "severity": "CRITICAL"}
    await target_for(rec).handle(kind="alarm_escalated", alarm=hot,
                                 print_=PRINT, link=open_link(), now=NOW)
    assert rec.body("PUT", f"{API}/issue/DCOPS-142")["fields"]["priority"] \
        == {"name": "Highest"}
    assert f"{API}/issue/DCOPS-142/comment" in rec.paths("POST")


async def test_a_clear_comments_and_flips_the_back_link_to_resolved():
    rec = Recorder()
    cleared = {**ALARM, "cleared_at": NOW}
    await target_for(rec).handle(kind="alarm_cleared", alarm=cleared,
                                 print_=PRINT, link=open_link(), now=NOW)
    assert f"{API}/issue/DCOPS-142/comment" in rec.paths("POST")
    assert rec.body("POST", f"{API}/issue/DCOPS-142/remotelink")[
        "object"]["status"] == {"resolved": True}


async def test_a_clear_does_not_transition_by_default():
    """A fault clearing is not the same as the work being finished - somebody
    still has to replace the fan the CRAH is now running without."""
    rec = Recorder()
    update = await target_for(rec).handle(
        kind="alarm_cleared", alarm=ALARM, print_=PRINT, link=open_link(),
        now=NOW)
    assert f"{API}/issue/DCOPS-142/transitions" not in rec.paths("POST")
    assert update.closed_at is None


async def test_a_clear_transitions_when_the_site_asked_for_it():
    rec = Recorder({f"GET {API}/issue/DCOPS-142/transitions": {
        "transitions": [{"id": "31", "name": "Done",
                         "to": {"name": "Done",
                                "statusCategory": {"key": "done"}}}]}})
    update = await target_for(rec, close_on_clear="transition").handle(
        kind="alarm_cleared", alarm=ALARM, print_=PRINT, link=open_link(),
        now=NOW)
    assert rec.body("POST", f"{API}/issue/DCOPS-142/transitions") == {
        "transition": {"id": "31"}}
    assert update.closed_at == NOW


async def test_a_transition_the_workflow_does_not_offer_is_not_an_error():
    """Somebody moved the issue by hand, or the workflow has a gate. The
    comment landed; forcing a transition a workflow refuses is not this
    integration's business."""
    rec = Recorder({f"GET {API}/issue/DCOPS-142/transitions": {"transitions": []}})
    update = await target_for(rec, close_on_clear="transition").handle(
        kind="alarm_cleared", alarm=ALARM, print_=PRINT, link=open_link(),
        now=NOW)
    assert update.issue_key == "DCOPS-142" and update.closed_at is None


# -------------------------------------------------------------- suppression

async def test_an_update_inside_the_quiet_window_is_skipped():
    rec = Recorder()
    link = open_link(last_pushed_at=NOW - timedelta(seconds=60))
    got = await target_for(rec, suppress_window_s=600).handle(
        kind="alarm_escalated", alarm=ALARM, print_=PRINT, link=link, now=NOW)
    assert got is None and rec.calls == []


async def test_a_clear_is_never_suppressed():
    """It is the message that ends the conversation."""
    rec = Recorder()
    link = open_link(last_pushed_at=NOW - timedelta(seconds=1))
    got = await target_for(rec, suppress_window_s=600).handle(
        kind="alarm_cleared", alarm=ALARM, print_=PRINT, link=link, now=NOW)
    assert got is not None and rec.calls


async def test_an_escalation_to_critical_is_never_suppressed():
    """The window hides noise. A condition becoming critical is not noise."""
    rec = Recorder()
    link = open_link(last_pushed_at=NOW - timedelta(seconds=1))
    got = await target_for(rec, suppress_window_s=600).handle(
        kind="alarm_escalated", alarm={**ALARM, "severity": "CRITICAL"},
        print_=PRINT, link=link, now=NOW)
    assert got is not None


# ------------------------------------------------------------ closed ticket

def closed_link(**over):
    base = open_link(closed_at=NOW - timedelta(hours=2), status_category="done")
    base.update(over)
    return base


REOPENABLE = {"transitions": [
    {"id": "11", "name": "Reopen",
     "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}}]}


async def test_a_recurrence_inside_the_window_reopens_rather_than_duplicating():
    rec = Recorder({f"GET {API}/issue/DCOPS-142/transitions": REOPENABLE})
    update = await target_for(rec, reopen_window_h=24).handle(
        kind="alarm_raised", alarm=ALARM, print_=PRINT, link=closed_link(),
        now=NOW)
    assert update.reopened is True and update.issue_key == "DCOPS-142"
    assert f"{API}/issue" not in rec.paths("POST")


async def test_a_recurrence_outside_the_window_gets_a_fresh_issue():
    """Past the window a condition has RECURRED rather than continued, and
    attaching it to a ticket somebody already reported on hides that."""
    rec = Recorder({f"POST {API}/issue": {"key": "DCOPS-200", "id": "10200"}})
    stale = closed_link(closed_at=NOW - timedelta(days=5))
    update = await target_for(rec, reopen_window_h=24).handle(
        kind="alarm_raised", alarm=ALARM, print_=PRINT, link=stale, now=NOW)
    assert update.issue_key == "DCOPS-200"


async def test_the_fresh_issue_is_linked_to_the_one_it_follows():
    rec = Recorder({f"POST {API}/issue": {"key": "DCOPS-200", "id": "10200"}})
    stale = closed_link(closed_at=NOW - timedelta(days=5))
    await target_for(rec, reopen_window_h=24).handle(
        kind="alarm_raised", alarm=ALARM, print_=PRINT, link=stale, now=NOW)
    body = rec.body("POST", f"{API}/issueLink")
    assert body["inwardIssue"] == {"key": "DCOPS-200"}
    assert body["outwardIssue"] == {"key": "DCOPS-142"}


async def test_a_declined_issue_is_never_reopened():
    """Won't Fix and Duplicate are decisions. Reopening one because the
    sensor fired again is how an integration gets turned off."""
    rec = Recorder({f"POST {API}/issue": {"key": "DCOPS-201", "id": "10201"}})
    update = await target_for(rec, reopen_window_h=24).handle(
        kind="alarm_raised", alarm=ALARM, print_=PRINT,
        link=closed_link(wont_reopen=True), now=NOW)
    assert update.issue_key == "DCOPS-201"
    assert f"{API}/issue/DCOPS-142/transitions" not in rec.paths("GET")


async def test_a_workflow_with_no_way_back_gets_a_fresh_issue():
    """Common on service projects. A new issue is the only honest option."""
    rec = Recorder({f"GET {API}/issue/DCOPS-142/transitions": {"transitions": []},
                    f"POST {API}/issue": {"key": "DCOPS-202", "id": "10202"}})
    update = await target_for(rec, reopen_window_h=24).handle(
        kind="alarm_raised", alarm=ALARM, print_=PRINT, link=closed_link(),
        now=NOW)
    assert update.issue_key == "DCOPS-202"


async def test_a_clear_on_an_already_closed_ticket_confirms_the_fault_is_gone():
    """The other half of "a closed ticket is not a cleared fault".

    A human closed this ticket without knowing whether the condition had
    actually gone - that is precisely why this integration refuses to treat
    their close as a clear. So when the plane does clear it, the ticket is
    told. No transition: it is already closed, and there is nothing to move.
    """
    rec = Recorder()
    update = await target_for(rec).handle(
        kind="alarm_cleared", alarm=ALARM, print_=PRINT, link=closed_link(),
        now=NOW)
    assert update.issue_key == "DCOPS-142"
    from app.integrations import adf as _adf
    text = _adf.to_text(
        rec.body("POST", f"{API}/issue/DCOPS-142/comment")["body"])
    assert "the plane confirms the fault is actually gone" in text
    assert f"{API}/issue/DCOPS-142/transitions" not in rec.paths("POST")
    # And the back-link flips, so the DCIM side reads as resolved too.
    assert rec.body("POST", f"{API}/issue/DCOPS-142/remotelink")[
        "object"]["status"] == {"resolved": True}


async def test_reopening_moves_out_of_done_by_category_not_by_name():
    """A customer who renamed Done to Resolved, or Reopen to Back to triage,
    would break any comparison against the name."""
    rec = Recorder({f"GET {API}/issue/DCOPS-142/transitions": {"transitions": [
        {"id": "5", "name": "Close as duplicate",
         "to": {"name": "Resolved", "statusCategory": {"key": "done"}}},
        {"id": "9", "name": "Back to triage",
         "to": {"name": "Triage", "statusCategory": {"key": "new"}}}]}})
    await target_for(rec, reopen_window_h=24).handle(
        kind="alarm_raised", alarm=ALARM, print_=PRINT, link=closed_link(),
        now=NOW)
    assert rec.body("POST", f"{API}/issue/DCOPS-142/transitions") == {
        "transition": {"id": "9"}}


# --------------------------------------------------------------- recovery

async def test_a_retry_searches_before_it_creates():
    """A dispatcher can die between Jira accepting a create and our own
    commit recording the key. Without this the next attempt makes a twin."""
    rec = Recorder({f"POST {API}/search/jql": {
        "issues": [{"key": "DCOPS-142", "id": "10142"}]}})
    update = await target_for(rec).handle(
        kind="alarm_raised", alarm=ALARM, print_=PRINT, link=None, attempts=2,
        now=NOW)
    assert update.issue_key == "DCOPS-142"
    assert f"{API}/issue" not in rec.paths("POST")


async def test_the_first_attempt_does_not_search():
    """Searching on every alarm is one JQL call per condition against an
    hourly point quota, on the path that runs hardest during a storm."""
    rec = Recorder({f"POST {API}/issue": CREATED})
    await target_for(rec).handle(kind="alarm_raised", alarm=ALARM,
                                 print_=PRINT, link=None, attempts=1, now=NOW)
    assert f"{API}/search/jql" not in rec.paths("POST")


async def test_the_recovery_search_names_its_fields_explicitly():
    """The replacement search endpoint no longer defaults `fields`; omitting
    it returns ids and almost nothing else, which reads as "no status"."""
    rec = Recorder({f"POST {API}/search/jql": {"issues": []},
                    f"POST {API}/issue": CREATED})
    await target_for(rec).handle(kind="alarm_raised", alarm=ALARM,
                                 print_=PRINT, link=None, attempts=2, now=NOW)
    body = rec.body("POST", f"{API}/search/jql")
    assert body["fields"] == ["key", "status", "resolution"]
    assert fp.label(PRINT) in body["jql"] and 'project = "DCOPS"' in body["jql"]


async def test_a_search_that_finds_nothing_still_creates():
    rec = Recorder({f"POST {API}/search/jql": {"issues": []},
                    f"POST {API}/issue": CREATED})
    update = await target_for(rec).handle(
        kind="alarm_raised", alarm=ALARM, print_=PRINT, link=None, attempts=3,
        now=NOW)
    assert update.issue_key == "DCOPS-142"


# ------------------------------------------------------------- service desk

SD = {"service_desk_id": "10", "request_type_id": "25"}


async def test_a_service_project_creates_through_the_request_api():
    """An issue created through /rest/api/3/issue on a service project has no
    request type, so it is malformed on the customer portal."""
    rec = Recorder({"POST /rest/servicedeskapi/request":
                    {"issueKey": "SD-7", "issueId": "10307"}})
    update = await target_for(rec, **SD).handle(
        kind="alarm_raised", alarm=ALARM, print_=PRINT, link=None, now=NOW)
    assert update.issue_key == "SD-7"
    assert f"{API}/issue" not in rec.paths("POST")


async def test_the_request_carries_the_desk_and_request_type():
    rec = Recorder({"POST /rest/servicedeskapi/request":
                    {"issueKey": "SD-7", "issueId": "10307"}})
    await target_for(rec, **SD).handle(kind="alarm_raised", alarm=ALARM,
                                       print_=PRINT, link=None, now=NOW)
    body = rec.body("POST", "/rest/servicedeskapi/request")
    assert body["serviceDeskId"] == "10" and body["requestTypeId"] == "25"


async def test_the_request_description_is_plain_text_by_default():
    """The sources disagree about whether servicedeskapi still wants a plain
    string where /rest/api/3/issue demands ADF. One document, rendered either
    way, and a setting rather than a guess."""
    rec = Recorder({"POST /rest/servicedeskapi/request":
                    {"issueKey": "SD-7", "issueId": "10307"}})
    await target_for(rec, **SD).handle(kind="alarm_raised", alarm=ALARM,
                                       print_=PRINT, link=None, now=NOW)
    body = rec.body("POST", "/rest/servicedeskapi/request")
    assert isinstance(body["requestFieldValues"]["description"], str)


async def test_the_request_description_can_be_adf_when_the_tenant_wants_it():
    rec = Recorder({"POST /rest/servicedeskapi/request":
                    {"issueKey": "SD-7", "issueId": "10307"}})
    await target_for(rec, jsm_description_adf=True, **SD).handle(
        kind="alarm_raised", alarm=ALARM, print_=PRINT, link=None, now=NOW)
    body = rec.body("POST", "/rest/servicedeskapi/request")
    assert body["requestFieldValues"]["description"]["type"] == "doc"


async def test_labels_are_applied_to_the_issue_the_request_created():
    """The request API only accepts fields the request type's form exposes,
    and labels almost never are."""
    rec = Recorder({"POST /rest/servicedeskapi/request":
                    {"issueKey": "SD-7", "issueId": "10307"}})
    await target_for(rec, **SD).handle(kind="alarm_raised", alarm=ALARM,
                                       print_=PRINT, link=None, now=NOW)
    assert fp.label(PRINT) in rec.body("PUT", f"{API}/issue/SD-7")["fields"]["labels"]


async def test_a_request_that_cannot_be_labelled_still_produces_a_ticket():
    rec = Recorder({"POST /rest/servicedeskapi/request":
                    {"issueKey": "SD-7", "issueId": "10307"},
                    f"PUT {API}/issue/SD-7": JiraError("no", status=403)})
    update = await target_for(rec, **SD).handle(
        kind="alarm_raised", alarm=ALARM, print_=PRINT, link=None, now=NOW)
    assert update.issue_key == "SD-7"


# ------------------------------------------------------------------ errors

async def test_a_failed_create_propagates_so_the_dispatcher_can_schedule_it():
    """The target never retries: only the dispatcher holds the durable
    attempt count, and a retry loop in both would turn eight into sixty-four."""
    rec = Recorder({f"POST {API}/issue": JiraError("rate limited", status=429,
                                                   retry_after=30.0)})
    with pytest.raises(JiraError) as caught:
        await target_for(rec).handle(kind="alarm_raised", alarm=ALARM,
                                     print_=PRINT, link=None, now=NOW)
    assert caught.value.retry_after == 30.0


# --------------------------------------------------------- change requests

WINDOW = {
    "id": "7c9e1a44-0000-4000-8000-000000000001",
    "title": "Replace CRAH01 fan tray", "description": "Fan 2 is out.",
    "kind": "planned", "starts_at": NOW, "ends_at": NOW + timedelta(hours=4),
    "created_by": "hari", "suppress": True, "status": "scheduled",
    "jira_issue_key": None, "require_approval": True,
}
WINDOW_PAYLOAD = {
    "window": WINDOW,
    "targets": [{"id": "d1", "name": "CRAH01-DC1-HA",
                 "datacenter_code": "DC1", "room_name": "Server Hall A"}],
    "preview": {"cut_off": 12, "downstream_devices": 15,
                "alarms_currently_active": 43, "redundancy_warnings": []},
}
CHANGE = {"key": "DCOPS-900", "id": "10900"}


async def test_a_window_opens_a_change_request():
    rec = Recorder({f"POST {API}/issue": CHANGE})
    update = await target_for(rec).handle_window(
        kind="window_requested", window=WINDOW, payload=WINDOW_PAYLOAD)
    assert update.issue_key == "DCOPS-900"


async def test_the_change_request_uses_the_change_issue_type():
    """A change is not an incident, and on most Jira sites that is a different
    issue type with a different workflow."""
    rec = Recorder({f"POST {API}/issue": CHANGE})
    await target_for(rec, change_issue_type="Change").handle_window(
        kind="window_requested", window=WINDOW, payload=WINDOW_PAYLOAD)
    fields = rec.body("POST", f"{API}/issue")["fields"]
    assert fields["issuetype"] == {"name": "Change"}
    assert "dcim-change" in fields["labels"]


async def test_the_change_request_carries_the_window_id_as_a_property():
    rec = Recorder({f"POST {API}/issue": CHANGE})
    await target_for(rec).handle_window(
        kind="window_requested", window=WINDOW, payload=WINDOW_PAYLOAD)
    prop = rec.body("POST", f"{API}/issue")["properties"][0]
    assert prop["key"] == "dcim.window"
    assert prop["value"]["window_id"] == WINDOW["id"]


async def test_a_window_that_already_has_a_change_request_does_not_open_a_second():
    """A redelivery, or somebody pressing the button twice. Two change
    requests for one window is worse than none - a board would approve one and
    the DCIM would be watching the other."""
    rec = Recorder({f"POST {API}/issue": CHANGE})
    got = await target_for(rec).handle_window(
        kind="window_requested", window={**WINDOW, "jira_issue_key": "DCOPS-1"},
        payload=WINDOW_PAYLOAD)
    assert got is None and rec.calls == []


async def test_the_completion_report_is_a_comment_on_the_change_request():
    rec = Recorder()
    window = {**WINDOW, "jira_issue_key": "DCOPS-900"}
    update = await target_for(rec).handle_window(
        kind="window_completed", window=window,
        payload={"window": window, "report": {
            "shelved": 43, "cleared": 40, "outcome": "completed",
            "still_open": [{"device_name": "CRAH02", "severity": "MAJOR",
                            "message": "Supply air 29C"}]}})
    assert update.issue_key == "DCOPS-900"
    body = rec.body("POST", f"{API}/issue/DCOPS-900/comment")["body"]
    from app.integrations import adf as _adf
    text = _adf.to_text(body)
    assert "43 alarms were shelved" in text and "CRAH02" in text


async def test_a_window_with_no_change_request_reports_to_nobody():
    """Not an error: a window can be completed having never asked for one."""
    rec = Recorder()
    got = await target_for(rec).handle_window(
        kind="window_completed", window=WINDOW,
        payload={"window": WINDOW, "report": {}})
    assert got is None and rec.calls == []


async def test_a_cancelled_window_says_no_work_was_done():
    rec = Recorder()
    window = {**WINDOW, "jira_issue_key": "DCOPS-900"}
    await target_for(rec).handle_window(
        kind="window_cancelled", window=window, payload={"window": window})
    from app.integrations import adf as _adf
    text = _adf.to_text(rec.body("POST", f"{API}/issue/DCOPS-900/comment")["body"])
    assert "cancelled in the DCIM" in text and "no alarms were shelved" in text


async def test_a_service_project_opens_the_change_as_a_request():
    rec = Recorder({"POST /rest/servicedeskapi/request":
                    {"issueKey": "SD-900", "issueId": "10900"}})
    update = await target_for(rec, service_desk_id="10", request_type_id="25",
                              change_request_type_id="31").handle_window(
        kind="window_requested", window=WINDOW, payload=WINDOW_PAYLOAD)
    assert update.issue_key == "SD-900"
    body = rec.body("POST", "/rest/servicedeskapi/request")
    # The CHANGE request type, not the incident one.
    assert body["requestTypeId"] == "31"


async def test_a_failed_back_link_does_not_lose_the_change_request():
    rec = Recorder({f"POST {API}/issue": CHANGE,
                    f"POST {API}/issue/DCOPS-900/remotelink":
                        JiraError("nope", status=400)})
    update = await target_for(rec).handle_window(
        kind="window_requested", window=WINDOW, payload=WINDOW_PAYLOAD)
    assert update.issue_key == "DCOPS-900"
