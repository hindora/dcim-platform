"""Configuration: sparse in, sparse out, and refused early.

The property worth protecting is that `validate` NEVER fills in defaults.
Writing the full document on first save is the mistake that freezes every
default at the version the integration was created under, so a default that
changes in a release never reaches the installs that were configured before
it. `collector_config` is sparse for the same reason.
"""

from __future__ import annotations

import pytest

from app.integrations.config import DEFAULTS, IntegrationConfigError, resolved, validate

# --------------------------------------------------------------- sparseness

def test_validate_does_not_fill_in_defaults():
    assert validate({"project_key": "DCOPS"}) == {"project_key": "DCOPS"}


def test_resolved_layers_the_document_over_the_defaults():
    out = resolved({"project_key": "DCOPS"})
    assert out["project_key"] == "DCOPS"
    assert out["issue_type"] == DEFAULTS["issue_type"]


def test_one_policy_clause_does_not_drop_the_others():
    """Merged per section rather than replaced. A UI that sends only the
    clause it changed must not silently disable the other five."""
    out = resolved({"policy": {"min_severity": "MINOR"}})
    assert out["policy"]["min_severity"] == "MINOR"
    assert out["policy"]["exclude_symptoms"] is True
    assert out["policy"]["response_classes"] == ["alarm"]


def test_resolved_does_not_mutate_the_defaults():
    """A shallow copy here would let one integration's saved policy leak into
    every other integration in the process."""
    resolved({"policy": {"min_severity": "INFO"}})
    assert DEFAULTS["policy"]["min_severity"] == "MAJOR"


def test_a_null_falls_back_rather_than_storing_null():
    assert validate({"issue_type": None}) == {}


# ---------------------------------------------------------------- refusals

def test_an_unknown_setting_is_refused():
    with pytest.raises(IntegrationConfigError, match="not an integration setting"):
        validate({"jira_url": "https://x"})


def test_an_unknown_policy_clause_is_refused():
    with pytest.raises(IntegrationConfigError, match="not a policy clause"):
        validate({"policy": {"min_sevrity": "MAJOR"}})


def test_an_unknown_category_is_refused():
    with pytest.raises(IntegrationConfigError, match="unknown categories"):
        validate({"policy": {"categories": ["power", "plumbing"]}})


def test_a_policy_that_matches_nothing_is_refused():
    """An empty list reads as "none of them", which disables ticketing while
    the UI still shows the integration enabled. Whoever wants that turns the
    integration off, where it is visible."""
    with pytest.raises(IntegrationConfigError, match="disable ticketing"):
        validate({"policy": {"categories": []}})
    with pytest.raises(IntegrationConfigError, match="disable ticketing"):
        validate({"policy": {"response_classes": []}})


def test_clear_is_not_a_severity_floor():
    """A floor of CLEAR would ticket every alarm that ever closed."""
    with pytest.raises(IntegrationConfigError, match="min_severity"):
        validate({"policy": {"min_severity": "CLEAR"}})


def test_a_label_prefix_cannot_contain_spaces():
    """Jira labels cannot hold whitespace; one sneaking in fails every create
    with a field error naming the label and not the setting behind it."""
    with pytest.raises(IntegrationConfigError, match="prefix cannot contain spaces"):
        validate({"labels": {"prefix": "dcim alarms"}})


def test_a_mapped_field_must_be_a_custom_field_id():
    """A human typing the field's NAME is the common mistake, and it fails at
    create time with an error that reads like a Jira outage."""
    with pytest.raises(IntegrationConfigError, match="customfield_"):
        validate({"fields": {"device": "Device name"}})
    assert validate({"fields": {"device": "customfield_10101"}}) \
        == {"fields": {"device": "customfield_10101"}}


def test_an_unmapped_field_may_be_cleared_to_empty():
    assert validate({"fields": {"device": ""}}) == {"fields": {"device": ""}}


def test_close_on_clear_is_one_of_three():
    with pytest.raises(IntegrationConfigError, match="close_on_clear"):
        validate({"close_on_clear": "delete"})


def test_the_reopen_window_is_bounded_at_a_week():
    """Past a day or so a condition has RECURRED rather than continued, and
    attaching a Tuesday fault to a Monday ticket somebody already reported on
    is how the ticket stops describing anything."""
    assert validate({"reopen_window_h": 168}) == {"reopen_window_h": 168}
    with pytest.raises(IntegrationConfigError, match="between 0 and 168"):
        validate({"reopen_window_h": 900})


def test_the_rate_limit_cannot_be_set_above_jiras_own():
    with pytest.raises(IntegrationConfigError, match="between 1 and 20"):
        validate({"rate_limit_rps": 500})


def test_a_storm_threshold_below_two_is_refused():
    """At one, the brake fires on an ordinary correlated pair."""
    with pytest.raises(IntegrationConfigError, match="between 2 and 1000"):
        validate({"storm_threshold": 1})


def test_categories_come_back_in_the_canonical_order():
    """So two equivalent documents compare equal and a save does not look
    like a change to anyone diffing."""
    out = validate({"policy": {"categories": ["network", "power"]}})
    assert out["policy"]["categories"] == ["power", "network"]


def test_a_number_arriving_as_text_is_accepted():
    """HTML number inputs send strings."""
    assert validate({"max_attempts": "5"}) == {"max_attempts": 5}


def test_a_boolean_setting_refuses_a_string():
    with pytest.raises(IntegrationConfigError, match="on or off"):
        validate({"components_enabled": "yes"})
