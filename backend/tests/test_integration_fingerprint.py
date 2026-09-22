"""The dedup key, and the four things it must ignore.

Every property here is one that, if it broke, would produce a service desk
full of duplicates for one fault - and would look completely normal from
inside the DCIM, because the alarm side would still be correct.
"""

from __future__ import annotations

from app.integrations import fingerprint as fp

BASE = {
    "device_id": "9f1d8a2e-0000-4000-8000-000000000001",
    "alarm_type": "inlet_temp_high",
    "instance": "probe-1",
    "datacenter_code": "DC1",
    "severity": "MAJOR",
    "message": "Inlet 28.4C above 26.0C",
    "trigger_value": 28.4,
    "first_seen": "2026-09-21T09:14:00+00:00",
}


def test_severity_does_not_change_the_fingerprint():
    """The property the whole design rests on.

    A WARNING that escalates to CRITICAL is the same condition getting worse.
    If severity were in the hash, the escalation would open a SECOND ticket
    and leave the first one open beside it - the exact pattern that makes an
    operator stop reading the queue.
    """
    warning = fp.of_alarm({**BASE, "severity": "WARNING"})
    critical = fp.of_alarm({**BASE, "severity": "CRITICAL"})
    assert warning == critical


def test_the_measured_value_does_not_change_it():
    """Otherwise every poll is a new fault."""
    assert fp.of_alarm({**BASE, "trigger_value": 28.4}) \
        == fp.of_alarm({**BASE, "trigger_value": 31.9})


def test_the_message_does_not_change_it():
    """Two detectors phrase one condition differently, and the alarm
    repository rewrites the message on its own conflict path."""
    assert fp.of_alarm(BASE) == fp.of_alarm({**BASE, "message": "over limit"})


def test_the_timestamp_does_not_change_it():
    assert fp.of_alarm(BASE) == fp.of_alarm(
        {**BASE, "first_seen": "2026-09-22T03:00:00+00:00"})


def test_the_instance_does_change_it():
    """Twenty-three Raritan conditions share one trap OID on this estate and
    are told apart only by the slot carried as the alarm instance. Collapsing
    them would put one ticket on the desk saying "Airflow Alert"."""
    assert fp.of_alarm({**BASE, "instance": "probe-1"}) \
        != fp.of_alarm({**BASE, "instance": "probe-2"})


def test_the_alarm_type_does_change_it():
    assert fp.of_alarm(BASE) != fp.of_alarm({**BASE, "alarm_type": "fan_failed"})


def test_the_device_does_change_it():
    other = "9f1d8a2e-0000-4000-8000-000000000002"
    assert fp.of_alarm(BASE) != fp.of_alarm({**BASE, "device_id": other})


def test_the_site_is_part_of_it():
    """Two datacentres run identical equipment under identical naming."""
    assert fp.of_alarm(BASE) != fp.of_alarm({**BASE, "datacenter_code": "DC2"})


def test_the_separator_cannot_be_forged_across_parts():
    """("a", "bc") and ("ab", "c") must not hash alike.

    A colon separator would fail this: BACnet object names and endpoint UUIDs
    both contain colons, so a crafted instance could impersonate another
    condition's key. The separator is a NUL, which appears in none of them.
    """
    assert fp.compute(device_id="d", alarm_type="a", instance="bc") \
        != fp.compute(device_id="d", alarm_type="ab", instance="c")


def test_a_platform_alarm_has_no_device_and_still_fingerprints():
    """Conditions that say this platform's own monitoring is broken are real
    conditions and deserve tickets. There is one ingest pipeline, so "ingest
    has stalled" is one fault however many times it is noticed."""
    one = fp.compute(device_id=None, alarm_type="ingest_stalled",
                     instance="telemetry.v1")
    two = fp.compute(device_id=None, alarm_type="ingest_stalled",
                     instance="telemetry.v1")
    assert one == two and len(one) == fp.LENGTH


def test_whitespace_and_case_are_normalised_except_on_the_instance():
    """Case folding the instance would merge two genuinely different
    conditions: an ifIndex is a number but a BACnet object name is not, and at
    least one of them is case-significant on the wire."""
    assert fp.compute(device_id="D", alarm_type="a", instance="x") \
        == fp.compute(device_id=" d ", alarm_type=" a ", instance=" x ")
    assert fp.compute(device_id="d", alarm_type="a", instance="X") \
        != fp.compute(device_id="d", alarm_type="a", instance="x")


def test_the_label_is_searchable_and_prefixed():
    print_ = fp.of_alarm(BASE)
    assert fp.label(print_) == f"fp-{print_}"
    # Jira labels cannot contain whitespace; a hex digest never does, but the
    # assertion is what stops somebody widening `LENGTH` into base64 later.
    assert not any(c.isspace() for c in fp.label(print_))


def test_the_global_id_survives_a_trailing_slash():
    """It is Jira's upsert key for the back-link. Two spellings of the same
    base URL would produce two links on one issue."""
    a = fp.global_id("https://dcim.example.com/", "abc")
    b = fp.global_id("https://dcim.example.com", "abc")
    assert a == b == "system=https://dcim.example.com&alarm=abc"


def test_the_version_tag_is_in_the_hash(monkeypatch):
    """A v2 fingerprint must not collide with a v1 one.

    The tag is what lets the identity tuple change one day without silently
    reopening every old ticket as a new condition - so it has to be part of
    the hashed input, not just a constant sitting in the module.
    """
    before = fp.compute(device_id="d", alarm_type="a", instance="x")
    monkeypatch.setattr(fp, "VERSION", "v2")
    assert fp.compute(device_id="d", alarm_type="a", instance="x") != before
