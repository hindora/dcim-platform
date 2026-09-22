"""A maintenance window, rendered as a change request.

The test that matters is the first one: the change request carries what the
window COSTS. That number - how many alarms it silences, how many machines it
darkens, which redundant side it removes - is the reason this is worth building
at all, and the DCIM is the only system that can compute it. A change request
without it is a title and a time range.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.integrations import adf, change

STARTS = datetime(2026, 9, 24, 2, 0, tzinfo=UTC)

WINDOW = {
    "id": "7c9e1a44-0000-4000-8000-000000000001",
    "title": "Replace CRAH01 fan tray",
    "description": "Fan 2 has been out since Tuesday.",
    "kind": "planned",
    "starts_at": STARTS,
    "ends_at": STARTS + timedelta(hours=4),
    "created_by": "hari",
    "suppress": True,
    "status": "scheduled",
}

PREVIEW = {
    "devices": 3, "downstream_devices": 15, "cut_off": 12,
    "alarms_currently_active": 43,
    "redundancy_warnings": [
        {"device": "SRV07-DC1-HA-R2-01", "detail": "left on a single feed"}],
}

TARGETS = [
    {"id": "d1", "name": "CRAH01-DC1-HA", "datacenter_code": "DC1",
     "room_name": "Server Hall A", "rack_name": None},
    {"id": "d2", "name": "SRV07-DC1-HA-R2-01", "datacenter_code": "DC1",
     "room_name": "Server Hall A", "rack_name": "R02"},
]


def flat(doc):
    return adf.to_text(doc)


# ------------------------------------------------------- what a board reads

def test_the_change_request_carries_what_the_window_costs():
    """The whole reason this exists. Nlyte does this against ServiceNow;
    against Jira no surveyed product does it at all."""
    text = flat(change.description(WINDOW, PREVIEW, TARGETS))
    assert "Alarms it will silence	43" in text
    assert "Machines it darkens	12" in text
    assert "Equipment	2" in text
    # The reason for the work, in the requester's own words, first.
    assert text.startswith("Fan 2 has been out since Tuesday.")


def test_the_degraded_count_is_downstream_minus_cut_off():
    """Two different events. A window that darkens twelve machines must not
    read the same as one that costs three of them a redundant side."""
    text = flat(change.description(WINDOW, PREVIEW, TARGETS))
    assert "Machines it leaves degraded\t3" in text


def test_a_redundancy_warning_comes_before_the_device_list():
    """The single most important thing on the page. A board that has to
    scroll past forty device names to find it is a board that stops looking
    for it."""
    text = flat(change.description(WINDOW, PREVIEW, TARGETS))
    assert text.index("single feed") < text.index("Equipment in this window")


def test_alarm_suppression_being_off_is_stated_not_implied():
    quiet = flat(change.description({**WINDOW, "suppress": False},
                                    PREVIEW, TARGETS))
    assert "OFF" in quiet
    loud = flat(change.description(WINDOW, PREVIEW, TARGETS))
    assert "will not page" in loud


def test_every_device_carries_its_location():
    """A list of forty names and no locations is a list the engineer has to
    bring back to the DCIM before they can walk anywhere."""
    text = flat(change.description(WINDOW, PREVIEW, TARGETS))
    assert "DC1 / Server Hall A / R02" in text


def test_a_long_device_list_is_truncated_with_a_count():
    many = [{"id": f"d{n}", "name": f"SRV{n:03d}"} for n in range(120)]
    text = flat(change.description(WINDOW, PREVIEW, many))
    assert "and 60 more" in text


def test_the_summary_names_the_kind_of_change():
    assert change.summary(WINDOW).startswith("[PLANNED CHANGE] ")
    assert change.summary({**WINDOW, "kind": "emergency"}).startswith(
        "[EMERGENCY CHANGE] ")


def test_the_duration_is_readable():
    text = flat(change.description(WINDOW, PREVIEW, TARGETS))
    assert "4h" in text


# --------------------------------------------------------------- approval

@pytest.mark.parametrize("status", ["Approved", "approved", "SCHEDULED",
                                    "Implementing", "Authorised"])
def test_statuses_that_mean_yes(status):
    assert change.decide(status) == change.APPROVED


@pytest.mark.parametrize("status", ["Declined", "Rejected", "Cancelled",
                                    "not approved"])
def test_statuses_that_mean_no(status):
    assert change.decide(status) == change.DECLINED


@pytest.mark.parametrize("status", ["Draft", "Awaiting CAB", "Triage", "", None])
def test_anything_else_says_nothing_rather_than_guessing(status):
    """A change moving from Draft to Awaiting CAB is a real transition that
    means approval has NOT been granted. Treating every unrecognised status as
    a refusal would cancel windows for paperwork."""
    assert change.decide(status) is None


def test_the_status_lists_are_configurable():
    assert change.decide("Go", approved=("go",)) == change.APPROVED
    assert change.decide("No go", declined=("no go",)) == change.DECLINED


def test_declined_wins_over_approved_when_a_name_is_in_both():
    """A misconfiguration, and the safe direction is to refuse: a window that
    does not open costs a trip; one that opens without permission shelves
    alarms on equipment nobody is touching."""
    assert change.decide("Closed", approved=("closed",),
                         declined=("closed",)) == change.DECLINED


def test_the_category_is_deliberately_not_used():
    """Every other branch in this package reads the status CATEGORY because
    names vary per workflow. Here the categories are useless: "Awaiting
    approval", "Approved" and "Implementing" are all `indeterminate`."""
    import inspect
    source = inspect.getsource(change.decide)
    assert "category" not in source


# ------------------------------------------------------------- completion

def test_the_completion_comment_lists_what_is_still_wrong():
    """"Did anything ELSE break while we were in there" is the question asked
    after every window, and the shelved set is the only place it can be
    answered - an alarm raised DURING the work is invisible by design."""
    report = {"shelved": 43, "cleared": 40, "outcome": "completed",
              "still_open": [{"device_name": "CRAH02-DC1-HA",
                              "severity": "MAJOR",
                              "message": "Supply air 29C"}]}
    text = flat(change.completion_comment(WINDOW, report))
    assert "43 alarms were shelved; 40 cleared" in text
    assert "CRAH02-DC1-HA" in text and "Supply air 29C" in text


def test_a_clean_window_says_so_plainly():
    report = {"shelved": 12, "cleared": 12, "outcome": "completed",
              "still_open": []}
    text = flat(change.completion_comment(WINDOW, report))
    assert "Nothing on this equipment is alarming now." in text


def test_one_open_condition_is_not_pluralised():
    report = {"shelved": 2, "cleared": 1, "outcome": "completed",
              "still_open": [{"device_name": "X", "severity": "MINOR",
                              "message": "y"}]}
    text = flat(change.completion_comment(WINDOW, report))
    assert "1 condition on this equipment is still open" in text
    assert "2 alarms were shelved" in text


def test_a_cancelled_window_reports_its_outcome():
    report = {"shelved": 0, "cleared": 0, "outcome": "cancelled",
              "still_open": []}
    assert "cancelled" in flat(change.completion_comment(WINDOW, report))


def test_the_window_link_is_omitted_without_a_public_url():
    assert change.window_url(None, WINDOW) is None
    assert change.window_url("https://dcim.example.com/", WINDOW) \
        == f"https://dcim.example.com/assets/maintenance/{WINDOW['id']}"
