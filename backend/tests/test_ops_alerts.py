"""Alarms as JSM Operations alerts.

The contrast with `test_jira_target.py` is the point of this file. Everything
the issue target builds by hand - a fingerprint label, a link row, a recovery
search, a reopen window - exists because a Jira issue has no notion of "the
same problem again". An Operations alert does, server-side, keyed on `alias`.
So these tests are mostly about what is DELIBERATELY absent.

The three that protect real money are the priority ones: the Operations API
does not reject an unrecognised priority, it silently substitutes P3.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.integrations import fingerprint as fp
from app.integrations.config import IntegrationConfigError, resolved, validate
from app.integrations.jira.client import JiraError
from app.integrations.ops.target import OpsAlertTarget

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
CLOUD = "11111111-2222-3333-4444-555555555555"

ALARM = {
    "id": "0f2b7c1e-0000-4000-8000-000000000009",
    "device_id": "9f1d8a2e-0000-4000-8000-000000000001",
    "device_name": "CRAH01-DC1-HA", "device_type": "crah",
    "alarm_type": "supply_temp_high", "instance": "AI:12",
    "severity": "CRITICAL", "message": "Supply air 31.2C above 26.0C",
    "metric_key": "supply_temp_c", "trigger_value": 31.2, "threshold": 26.0,
    "category": "cooling", "detection": "threshold", "source": "bacnet",
    "datacenter_code": "DC1", "room_name": "Server Hall A", "rack_name": None,
    "u_start": None, "mgmt_ip": "10.52.11.9", "serial_number": None,
    "asset_tag": None, "first_seen": NOW - timedelta(hours=1),
    "last_seen": NOW, "occurrence_count": 2,
}
PRINT = fp.of_alarm(ALARM)


class Recorder:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls: list[tuple[str, str, object]] = []

    async def request(self, method, path, *, json_body=None, params=None,
                      issue_key=None, files=None):
        self.calls.append((method, path, json_body or params))
        handler = self.routes.get(f"{method} {path}")
        if isinstance(handler, Exception):
            raise handler
        return handler if handler is not None else {}

    async def get(self, path, **kw):
        return await self.request("GET", path, **kw)

    async def post(self, path, **kw):
        return await self.request("POST", path, **kw)

    def paths(self, method=None):
        return [p for m, p, _ in self.calls if method is None or m == method]

    def body(self, method, path):
        for m, p, b in self.calls:
            if m == method and p == path:
                return b
        raise AssertionError(f"no {method} {path} in {self.paths()}")


def target_for(recorder, **config):
    return OpsAlertTarget(recorder, resolved(config), cloud_id=CLOUD,
                          dcim_base="https://dcim.example.com")


BASE = f"https://api.atlassian.com/jsm/ops/api/{CLOUD}/v1"


# ------------------------------------------------------------- the alias

async def test_the_alias_is_the_fingerprint():
    """The whole reason this target is short. A create whose alias matches an
    OPEN alert de-duplicates server-side and bumps its count, so none of the
    issue path's dedup machinery is needed."""
    rec = Recorder()
    await target_for(rec).handle(kind="alarm_raised", alarm=ALARM, print_=PRINT)
    assert rec.body("POST", f"{BASE}/alerts")["alias"] == PRINT


async def test_a_create_needs_no_lookup_first():
    """Dedup is the API's job here. Searching before creating would be one
    extra call per alarm on the path that runs hardest during a storm."""
    rec = Recorder()
    await target_for(rec).handle(kind="alarm_raised", alarm=ALARM, print_=PRINT)
    assert rec.paths("GET") == []


async def test_a_re_raise_is_just_another_create():
    """There is no reopen window and no recovery search: a closed alias
    creates a new alert, which is exactly what a recurrence should do."""
    rec = Recorder()
    t = target_for(rec)
    await t.handle(kind="alarm_raised", alarm=ALARM, print_=PRINT)
    await t.handle(kind="alarm_raised", alarm=ALARM, print_=PRINT)
    assert rec.paths("POST").count(f"{BASE}/alerts") == 2


async def test_an_escalation_is_sent_as_a_create_with_the_new_priority():
    """A create against an OPEN alias de-duplicates and bumps the count, so
    one request carries the escalation and the alert's own history records
    it."""
    rec = Recorder()
    await target_for(rec).handle(kind="alarm_escalated", alarm=ALARM,
                                 print_=PRINT)
    body = rec.body("POST", f"{BASE}/alerts")
    assert body["priority"] == "P1"
    assert body["note"] == "Severity escalated"


# ----------------------------------------------------------- the priority

@pytest.mark.parametrize("severity,expected", [
    ("CRITICAL", "P1"), ("MAJOR", "P2"), ("MINOR", "P3"),
    ("WARNING", "P4"), ("INFO", "P5"),
])
async def test_severity_maps_to_a_p_number(severity, expected):
    rec = Recorder()
    await target_for(rec).handle(kind="alarm_raised",
                                 alarm={**ALARM, "severity": severity},
                                 print_=PRINT)
    assert rec.body("POST", f"{BASE}/alerts")["priority"] == expected


async def test_an_unmappable_severity_sends_no_priority_at_all():
    """NEVER a guess. The Operations API does not reject an unrecognised
    priority - it SILENTLY SUBSTITUTES P3 - so sending one this map does not
    cover would page the estate's worst faults at the same urgency as its
    mildest and nothing would say so. Omitting it lets the team's own default
    apply, which is at least a decision somebody made."""
    rec = Recorder()
    await target_for(rec).handle(kind="alarm_raised",
                                 alarm={**ALARM, "severity": "WEIRD"},
                                 print_=PRINT)
    assert "priority" not in rec.body("POST", f"{BASE}/alerts")


async def test_a_priority_outside_p1_to_p5_is_refused_rather_than_sent():
    """Belt and braces against the same silent substitution: even if a bad
    value reached the config, the target drops it."""
    rec = Recorder()
    t = target_for(rec, ops_priority_map={"CRITICAL": "Highest"})
    assert t.priority(ALARM) is None


def test_the_config_refuses_a_priority_the_api_would_flatten():
    """The loud failure that prevents the silent one."""
    with pytest.raises(IntegrationConfigError, match="silently substitutes P3"):
        validate({"ops_priority_map": {"CRITICAL": "Highest"}})
    assert validate({"ops_priority_map": {"CRITICAL": "p1"}}) \
        == {"ops_priority_map": {"CRITICAL": "P1"}}


# -------------------------------------------------------------- the alert

async def test_the_alert_carries_what_an_on_call_engineer_needs():
    rec = Recorder()
    await target_for(rec).handle(kind="alarm_raised", alarm=ALARM, print_=PRINT)
    body = rec.body("POST", f"{BASE}/alerts")
    assert body["message"].startswith("[CRAH01-DC1-HA]")
    assert body["entity"] == "CRAH01-DC1-HA"
    assert body["source"] == "DCIM Platform"
    assert "Supply air 31.2C" in body["description"]
    assert body["details"]["location"] == "DC1 / Server Hall A"


async def test_the_message_is_short_enough_for_a_phone():
    rec = Recorder()
    await target_for(rec).handle(
        kind="alarm_raised", alarm={**ALARM, "message": "x" * 400},
        print_=PRINT)
    assert len(rec.body("POST", f"{BASE}/alerts")["message"]) <= 130


async def test_the_description_is_plain_text_not_adf():
    """The Operations API is Opsgenie's, and predates ADF entirely."""
    rec = Recorder()
    await target_for(rec).handle(kind="alarm_raised", alarm=ALARM, print_=PRINT)
    assert isinstance(rec.body("POST", f"{BASE}/alerts")["description"], str)


async def test_responders_are_only_sent_when_configured():
    """Empty leaves routing to the team's own rules in JSM, which is usually
    what an on-call setup already encodes."""
    rec = Recorder()
    await target_for(rec).handle(kind="alarm_raised", alarm=ALARM, print_=PRINT)
    assert "responders" not in rec.body("POST", f"{BASE}/alerts")

    rec2 = Recorder()
    await target_for(rec2, ops_responders=["Facilities"]).handle(
        kind="alarm_raised", alarm=ALARM, print_=PRINT)
    assert rec2.body("POST", f"{BASE}/alerts")["responders"] == [
        {"type": "team", "name": "Facilities"}]


# --------------------------------------------------------------- clearing

async def test_a_clear_closes_the_alert():
    """Unlike an issue, where a clear only comments by default. An issue
    tracks WORK, which outlives the fault; an alert tracks the CONDITION, and
    one left open after the condition cleared keeps paging somebody about a
    fault that is over."""
    rec = Recorder({f"GET {BASE}/alerts/alias": {"data": {"id": "al-7"}}})
    update = await target_for(rec).handle(kind="alarm_cleared", alarm=ALARM,
                                          print_=PRINT)
    assert update.action == "closed"
    assert f"{BASE}/alerts/al-7/close" in rec.paths("POST")


async def test_a_clear_resolves_the_alias_before_acting():
    """A close against an alias matching no OPEN alert is DROPPED - not
    refused, not 404'd, dropped - so acting blind would report success for a
    page that is still ringing."""
    rec = Recorder({f"GET {BASE}/alerts/alias": {"data": {"id": "al-7"}}})
    await target_for(rec).handle(kind="alarm_cleared", alarm=ALARM,
                                 print_=PRINT)
    assert rec.body("GET", f"{BASE}/alerts/alias") == {"alias": PRINT}


async def test_a_clear_with_no_open_alert_is_not_an_error():
    """Already closed, or never created because the policy did not match at
    raise time. Both are normal and neither is worth a retry."""
    rec = Recorder({f"GET {BASE}/alerts/alias": JiraError("gone", status=404)})
    assert await target_for(rec).handle(kind="alarm_cleared", alarm=ALARM,
                                        print_=PRINT) is None
    assert rec.paths("POST") == []


async def test_a_real_failure_looking_up_the_alias_still_raises():
    """A 500 is an outage and must reach the dispatcher's retry schedule, not
    be swallowed as "no open alert"."""
    rec = Recorder({f"GET {BASE}/alerts/alias": JiraError("boom", status=500)})
    with pytest.raises(JiraError):
        await target_for(rec).handle(kind="alarm_cleared", alarm=ALARM,
                                     print_=PRINT)


async def test_acknowledging_stops_the_page():
    rec = Recorder({f"GET {BASE}/alerts/alias": {"data": {"id": "al-7"}}})
    update = await target_for(rec).acknowledge(PRINT, "Acked in the DCIM")
    assert update.action == "acknowledged"
    assert f"{BASE}/alerts/al-7/acknowledge" in rec.paths("POST")


# ---------------------------------------------------------------- wiring

async def test_the_alert_api_is_addressed_by_cloud_id_not_site_url():
    """The one configuration fact that differs from every other target."""
    rec = Recorder()
    await target_for(rec).handle(kind="alarm_raised", alarm=ALARM, print_=PRINT)
    assert rec.paths("POST")[0] == (
        f"https://api.atlassian.com/jsm/ops/api/{CLOUD}/v1/alerts")


def test_an_operations_integration_without_a_cloud_id_cannot_be_enabled():
    from app.services import integrations as service
    why = service.ready_to_enable({"kind": "jsm_ops", "config": {}})
    assert "cloud id" in why


def test_an_operations_integration_needs_no_project_key():
    """There is no project, no issue type and no request type, because an
    alert is not an issue."""
    from app.services import integrations as service
    assert service.ready_to_enable(
        {"kind": "jsm_ops", "cloud_id": CLOUD, "config": {}}) is None


def test_the_target_signature_matches_the_issue_targets():
    """So the dispatcher does not branch on which one it holds. `link` and
    `now` are accepted and ignored: this target keeps no link and has no
    reopen window."""
    import inspect

    from app.integrations.jira.target import IssueTarget
    ops = set(inspect.signature(OpsAlertTarget.handle).parameters)
    issues = set(inspect.signature(IssueTarget.handle).parameters)
    assert issues <= ops
