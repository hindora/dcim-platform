"""Jira Data Center is a different API, not a different hostname.

Everything in `test_jira_target.py` runs against Cloud, and Cloud is where
this integration was built. Data Center diverges in three ways that each fail
on the very first call rather than degrading:

  * it serves REST **v2** and has no v3 at all, so a v3 path is a flat 404;
  * it never had Cloud's October 2025 search change, so `/search/jql` does
    not exist there and `/search` does;
  * v2 takes `description` and `comment.body` as **strings**, so an ADF
    document is rejected or stored as literal JSON.

The Cloud assertions are repeated here deliberately. Every one of these is a
statement that the two deployments DIFFER, and a test that only pinned Data
Center would still pass if someone "simplified" Cloud onto v2.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.integrations.config import resolved
from app.integrations.jira.client import JiraError
from app.integrations.jira.target import IssueTarget, api_for, search_for

from .test_jira_target import ALARM, PRINT, Recorder

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
DC = "/rest/api/2"
CLOUD = "/rest/api/3"
CREATED = {"key": "DCOPS-142", "id": "10142"}
OPEN_LINK = {"issue_key": "DCOPS-142", "status_category": "in progress",
             "closed_at": None}


def dc_target(recorder, **config):
    cfg = resolved({"project_key": "DCOPS", "issue_type": "Incident", **config})
    return IssueTarget(recorder, cfg, base_url="https://jira.acme.internal",
                       dcim_base="https://dcim.example.com",
                       deployment="jira_dc")


def cloud_target(recorder, **config):
    cfg = resolved({"project_key": "DCOPS", "issue_type": "Incident", **config})
    return IssueTarget(recorder, cfg, base_url="https://acme.atlassian.net",
                       dcim_base="https://dcim.example.com",
                       deployment="jira_cloud")


# ------------------------------------------------------------ the version

def test_the_rest_version_follows_the_deployment():
    assert api_for("jira_dc") == DC
    assert api_for("jira_cloud") == CLOUD


def test_an_unknown_deployment_gets_cloud():
    """Cloud is the only one with a working ticket path, so an unrecognised
    value must not silently pick the other one."""
    assert api_for(None) == CLOUD
    assert api_for("something-new") == CLOUD


def test_data_center_searches_the_endpoint_it_actually_has():
    """Cloud's /search was removed in Oct 2025 and answers 410, which is why
    it moved to /search/jql. Data Center never had that change and so has no
    /search/jql to move to."""
    assert search_for("jira_dc") == DC + "/search"
    assert search_for("jira_cloud") == CLOUD + "/search/jql"


async def test_every_call_a_raise_makes_is_v2():
    rec = Recorder({"POST " + DC + "/issue": CREATED})
    await dc_target(rec).handle(kind="alarm_raised", alarm=ALARM,
                                print_=PRINT, link=None, now=NOW)
    assert rec.paths(), "nothing was called"
    for path in rec.paths():
        assert not path.startswith(CLOUD), path + " is a v3 path"


async def test_the_search_on_a_retry_uses_the_v2_endpoint():
    rec = Recorder({"POST " + DC + "/search": {"issues": []},
                    "POST " + DC + "/issue": CREATED})
    await dc_target(rec).handle(kind="alarm_raised", alarm=ALARM,
                                print_=PRINT, link=None, attempts=2, now=NOW)
    assert DC + "/search" in rec.paths("POST")
    assert CLOUD + "/search/jql" not in rec.paths("POST")


# --------------------------------------------------------- the body format

async def test_the_description_is_a_string_on_data_center():
    rec = Recorder({"POST " + DC + "/issue": CREATED})
    await dc_target(rec).handle(kind="alarm_raised", alarm=ALARM,
                                print_=PRINT, link=None, now=NOW)
    description = rec.body("POST", DC + "/issue")["fields"]["description"]
    assert isinstance(description, str), "ADF sent to a v2 endpoint"
    # Flattened, not emptied: the ticket still has to carry the story.
    assert "CRAH01-DC1-HA" in description


async def test_the_description_is_adf_on_cloud():
    rec = Recorder({"POST " + CLOUD + "/issue": CREATED})
    await cloud_target(rec).handle(kind="alarm_raised", alarm=ALARM,
                                   print_=PRINT, link=None, now=NOW)
    description = rec.body("POST", CLOUD + "/issue")["fields"]["description"]
    assert isinstance(description, dict)
    assert description.get("type") == "doc"


async def test_a_comment_is_a_string_on_data_center():
    rec = Recorder()
    await dc_target(rec).handle(kind="alarm_cleared", alarm=ALARM,
                                print_=PRINT, link=OPEN_LINK, now=NOW)
    body = rec.body("POST", DC + "/issue/DCOPS-142/comment")
    assert isinstance(body["body"], str)


async def test_a_comment_is_adf_on_cloud():
    rec = Recorder()
    await cloud_target(rec).handle(kind="alarm_cleared", alarm=ALARM,
                                   print_=PRINT, link=OPEN_LINK, now=NOW)
    body = rec.body("POST", CLOUD + "/issue/DCOPS-142/comment")
    assert isinstance(body["body"], dict)


async def test_a_service_desk_request_is_never_adf_on_data_center():
    """`jsm_description_adf` is a Cloud question. Honouring it against a v2
    tenant would send ADF to the one deployment that cannot read it."""
    rec = Recorder({"POST /rest/servicedeskapi/request":
                    {"issueKey": "DCOPS-9", "id": "9"}})
    t = dc_target(rec, service_desk_id="1", request_type_id="7",
                  jsm_description_adf=True)
    await t.handle(kind="alarm_raised", alarm=ALARM, print_=PRINT,
                   link=None, now=NOW)
    values = rec.body("POST",
                      "/rest/servicedeskapi/request")["requestFieldValues"]
    assert isinstance(values["description"], str)


# ---------------------------------------------------------- the properties

async def test_the_create_payload_carries_no_properties_on_data_center():
    """v2's create is not documented to accept them, and a create rejected
    over a field nobody reads loses the ticket itself."""
    rec = Recorder({"POST " + DC + "/issue": CREATED})
    await dc_target(rec).handle(kind="alarm_raised", alarm=ALARM,
                                print_=PRINT, link=None, now=NOW)
    assert "properties" not in rec.body("POST", DC + "/issue")


async def test_the_property_is_set_afterwards_on_data_center():
    rec = Recorder({"POST " + DC + "/issue": CREATED})
    await dc_target(rec).handle(kind="alarm_raised", alarm=ALARM,
                                print_=PRINT, link=None, now=NOW)
    path = DC + "/issue/DCOPS-142/properties/dcim.alarm"
    assert path in rec.paths("PUT")
    assert rec.body("PUT", path)["fingerprint"] == PRINT


async def test_a_refused_property_does_not_lose_the_ticket():
    """The label carries the same fingerprint and the recovery search reads
    THAT, so the integration still de-duplicates without the property."""
    rec = Recorder({
        "POST " + DC + "/issue": CREATED,
        "PUT " + DC + "/issue/DCOPS-142/properties/dcim.alarm":
            JiraError("no such field", status=400),
    })
    update = await dc_target(rec).handle(kind="alarm_raised", alarm=ALARM,
                                         print_=PRINT, link=None, now=NOW)
    assert update is not None and update.issue_key == "DCOPS-142"


async def test_cloud_still_sends_properties_inline():
    rec = Recorder({"POST " + CLOUD + "/issue": CREATED})
    await cloud_target(rec).handle(kind="alarm_raised", alarm=ALARM,
                                   print_=PRINT, link=None, now=NOW)
    props = rec.body("POST", CLOUD + "/issue")["properties"]
    assert props[0]["key"] == "dcim.alarm"
    assert CLOUD + "/issue/DCOPS-142/properties/dcim.alarm" \
        not in rec.paths("PUT")


# ------------------------------------------------------- the service wiring

async def test_the_connection_test_asks_data_center_v2():
    """Otherwise a perfectly good credential reports "authentication failed"
    and the operator goes after the wrong thing."""
    from app.services import integrations as service

    class Closable(Recorder):
        """`run_test` owns the client's lifetime and closes it in a finally."""

        async def aclose(self):
            return None

    rec = Closable({"GET " + DC + "/myself": {"displayName": "DCIM bot"}})
    await service.run_test({"kind": "jira_dc", "config": {}}, rec)
    assert DC + "/myself" in rec.paths("GET")
    assert CLOUD + "/myself" not in rec.paths("GET")
