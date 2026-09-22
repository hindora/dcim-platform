"""An alarm rendered as a Jira issue.

The assertions that matter are about what is NOT sent. A create naming a field
that is not on the project's create screen fails entirely, so an unmapped
field must be left out rather than sent as null - and a priority this site
does not have must be omitted rather than guessed, because a wrong priority
buries a CRITICAL under a password reset in a queue sorted by it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.integrations import fingerprint as fp
from app.integrations.config import resolved
from app.integrations.jira import mapping

CFG = resolved({"project_key": "DCOPS"})

ALARM = {
    "id": "0f2b7c1e-0000-4000-8000-000000000009",
    "device_id": "9f1d8a2e-0000-4000-8000-000000000001",
    "device_name": "CRAH01-DC1-HA",
    "device_type": "crah",
    "alarm_type": "supply_temp_high",
    "instance": "AI:12",
    "severity": "MAJOR",
    "message": "Supply air 28.4C above 26.0C for 15 min",
    "metric_key": "supply_temp_c",
    "trigger_value": 28.4,
    "threshold": 26.0,
    "category": "cooling",
    "detection": "threshold",
    "source": "bacnet",
    "datacenter_code": "DC1",
    "room_name": "Server Hall A",
    "rack_name": None,
    "u_start": None,
    "mgmt_ip": "10.52.11.9",
    "serial_number": None,
    "asset_tag": None,
    "first_seen": datetime(2026, 9, 22, 9, 14, tzinfo=UTC),
    "occurrence_count": 4,
}

PRINT = fp.of_alarm(ALARM)


# ---------------------------------------------------------------- summary

def test_the_summary_leads_with_the_device():
    """A service desk queue is read as a column of left-aligned text and the
    first characters are all most rows get."""
    assert mapping.summary(ALARM).startswith("[CRAH01-DC1-HA] ")


def test_the_instance_is_appended_only_when_the_message_omits_it():
    assert "(AI:12)" in mapping.summary(ALARM)
    carried = {**ALARM, "message": "Supply air high on AI:12"}
    assert mapping.summary(carried).count("AI:12") == 1


def test_a_long_summary_is_truncated_rather_than_rejected():
    """Jira refuses a summary over 255 with a field error naming the field,
    which reads like a mapping bug rather than a long message."""
    got = mapping.summary({**ALARM, "message": "x" * 500})
    assert len(got) == mapping.MAX_SUMMARY


def test_a_platform_alarm_with_no_device_still_gets_a_summary():
    got = mapping.summary({"alarm_type": "ingest_stalled",
                           "message": "No telemetry", "instance": ""})
    assert got.startswith("[platform]")


# ----------------------------------------------------------------- labels

def test_the_fingerprint_label_is_always_present():
    assert fp.label(PRINT) in mapping.labels(ALARM, CFG, PRINT)


def test_whitespace_in_a_room_name_becomes_a_hyphen():
    """Jira splits labels on whitespace in some entrypoints and errors in
    others; neither is what the caller meant."""
    labels = mapping.labels(ALARM, CFG, PRINT)
    assert "room-Server-Hall-A" in labels
    assert not any(" " in label for label in labels)


def test_labels_are_deduplicated_but_keep_their_order():
    cfg = resolved({"labels": {"prefix": "dcim", "category": False,
                               "site": False, "room": False}})
    assert mapping.labels(ALARM, cfg, PRINT) == ["dcim", fp.label(PRINT)]


def test_a_facet_can_be_switched_off():
    cfg = resolved({"labels": {"site": False}})
    assert not any(label.startswith("site-")
                   for label in mapping.labels(ALARM, cfg, PRINT))


# --------------------------------------------------------------- priority

def test_severity_maps_to_a_priority_name_not_an_id():
    """Ids are per-site; names are what Jira accepts and what a customer can
    rename in one place."""
    assert mapping.priority(ALARM, CFG) == "High"
    assert mapping.priority({**ALARM, "severity": "CRITICAL"}, CFG) == "Highest"


def test_an_unmapped_severity_yields_no_priority_at_all():
    assert mapping.priority({**ALARM, "severity": "WEIRD"}, CFG) is None
    assert "priority" not in mapping.fields_for(
        {**ALARM, "severity": "WEIRD"}, CFG, PRINT)


# ---------------------------------------------------------- custom fields

def test_an_unmapped_field_is_omitted_entirely():
    """Not sent as null: a create naming a field that is not on the project's
    screen fails the whole ticket over a nice-to-have."""
    assert mapping.custom_fields(ALARM, CFG) == {}


def test_a_mapped_field_is_sent_under_its_id():
    cfg = resolved({"fields": {"device": "customfield_10101",
                               "location": "customfield_10102",
                               "occurrences": "customfield_10104"}})
    out = mapping.custom_fields(ALARM, cfg)
    assert out["customfield_10101"] == "CRAH01-DC1-HA"
    assert out["customfield_10102"] == "DC1 / Server Hall A"
    assert out["customfield_10104"] == 4


def test_an_occurrence_update_carries_only_the_count():
    """One write, not a comment. Jira limits writes against a single issue to
    20 in two seconds, and a flapping condition aims the whole estate's
    traffic at one key."""
    cfg = resolved({"fields": {"device": "customfield_10101",
                               "occurrences": "customfield_10104"}})
    assert mapping.occurrence_fields(ALARM, cfg) == {"customfield_10104": 4}


def test_with_no_occurrence_field_there_is_nothing_to_update():
    assert mapping.occurrence_fields(ALARM, CFG) == {}


# --------------------------------------------------------------- location

def test_a_floor_standing_device_has_no_rack_in_its_location():
    assert mapping.location(ALARM) == "DC1 / Server Hall A"


def test_a_racked_device_carries_its_unit():
    racked = {**ALARM, "rack_name": "R03", "u_start": 12}
    assert mapping.location(racked) == "DC1 / Server Hall A / R03 U12"


# ------------------------------------------------------------ description

def test_the_description_carries_the_reading_and_the_raw_record():
    doc = mapping.description(ALARM)
    flat = _flat(doc)
    assert "supply_temp_c 28.4 against 26" in flat
    assert "supply_temp_high" in flat


def test_a_measurement_of_none_is_left_out_of_the_table():
    doc = mapping.description({**ALARM, "trigger_value": None})
    assert "against" not in _flat(doc)


def test_a_float_is_not_printed_with_binary_noise():
    """28.399999999999999 on a ticket is the kind of detail that makes a
    reader distrust everything else on it."""
    doc = mapping.description({**ALARM, "trigger_value": 28.4 + 1e-14})
    assert "28.399999" not in _flat(doc)


def test_the_dcim_link_is_omitted_when_no_public_url_is_configured():
    """A link to localhost on somebody else's service desk is worse than no
    link: it looks like it works."""
    assert mapping.alarm_url(None, ALARM) is None
    assert mapping.alarm_url("", ALARM) is None
    assert mapping.alarm_url("https://dcim.example.com/", ALARM) \
        == f"https://dcim.example.com/alarms?alarm={ALARM['id']}"


# ------------------------------------------------------------ remote link

def test_the_remote_link_is_keyed_for_upsert():
    """globalId is Jira's own upsert key, so posting it twice updates rather
    than duplicating - the one idempotency guard that survives a crash
    between the HTTP call and our own commit."""
    link = mapping.remote_link(ALARM, PRINT, base_url="https://dcim.example.com",
                               dcim_url="https://dcim.example.com/alarms?alarm=x",
                               resolved=False)
    assert link["globalId"] == fp.global_id("https://dcim.example.com", PRINT)
    assert link["object"]["status"] == {"resolved": False}


# --------------------------------------------------------------- comments

def test_a_clear_comment_says_the_plane_measured_it_not_that_work_is_done():
    """The distinction the whole inbound design rests on: a condition
    clearing is not the same as the work being finished."""
    flat = _flat(mapping.comment_for(
        "alarm_cleared", {**ALARM, "cleared_at": ALARM["first_seen"]}))
    assert "Cleared at" in flat
    assert "not a statement that the work is finished" in flat


def test_an_escalation_comment_names_the_new_severity_and_the_reading():
    flat = _flat(mapping.comment_for("alarm_escalated",
                                     {**ALARM, "severity": "CRITICAL",
                                      "last_seen": ALARM["first_seen"]}))
    assert "Escalated to CRITICAL" in flat and "supply_temp_c 28.4" in flat


def _flat(doc):
    from app.integrations import adf
    return adf.to_text(doc)
