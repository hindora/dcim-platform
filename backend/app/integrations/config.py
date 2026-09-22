"""What an integration can be told, its defaults, and what it may not be told.

`integration.config` is sparse on purpose, the same way `collector_config` is:
it holds only what an operator actually set, and everything absent falls
through to `DEFAULTS` here. That is what lets a default change in a release
reach every install that never overrode it - the alternative, writing the full
document on first save, freezes every default at the version it was created
under and nobody ever finds out.

`resolved()` is the only way the rest of the package reads configuration. No
module reaches into the raw JSONB and no module carries its own fallback,
because a default that exists in two places is a default that will diverge.
"""

from __future__ import annotations

import copy
from typing import Any

from app.core.alert_taxonomy import CATEGORIES, RESPONSE_CLASSES
from app.models.enums import Severity

#: Severity ranked worst-first, mirroring `_SEV_RANK` in the alarm repository.
#: Declared here rather than imported so that a policy comparison and a SQL
#: comparison cannot drift apart silently - the test asserts they agree.
SEVERITY_RANK: dict[str, int] = {
    Severity.CRITICAL: 0, Severity.MAJOR: 1, Severity.MINOR: 2,
    Severity.WARNING: 3, Severity.INFO: 4, Severity.CLEAR: 5,
}

DEFAULTS: dict[str, Any] = {
    # ------------------------------------------------------------- target
    "project_key": "",
    "issue_type": "Task",
    # JSM only. Set both and the service desk API is used instead of the
    # platform one, because an issue created without a request type lands in
    # the project but is malformed on the customer portal.
    "service_desk_id": "",
    "request_type_id": "",

    # ------------------------------------------------------------- change
    #
    # A maintenance window opens a CHANGE request, not an incident, and on
    # most Jira sites that is a different issue type with a different
    # workflow. Empty falls back to the incident settings above, which is
    # wrong-but-working rather than broken: the change lands, in the wrong
    # queue, where somebody will notice.
    "change_issue_type": "",
    "change_request_type_id": "",
    #: Which status NAMES mean a change was approved, and which mean it was
    #: refused.
    #:
    #: Names rather than the status CATEGORY, which is what every other branch
    #: in this package reads - and the exception is deliberate. A change
    #: workflow's categories are useless here: "Awaiting approval", "Approved"
    #: and "Implementing" are all `indeterminate`. An unrecognised status
    #: leaves the window exactly where it was rather than guessing.
    "change_approved_statuses": ["Approved", "Scheduled", "Implementing",
                                 "In Progress"],
    "change_declined_statuses": ["Declined", "Rejected", "Cancelled"],

    # ------------------------------------------------------------- policy
    #
    # Conservative on purpose. The escape hatch for everything this excludes is
    # POST /alarms/{id}/ticket - an operator who wants a ticket for a MINOR
    # gets one in a click, so the policy never has to be widened to cover a
    # judgement call.
    "policy": {
        # `alarm` demands action now and expects an acknowledgement; `alert`
        # is informational and belongs to whoever schedules the work. Only the
        # first is a ticket.
        "response_classes": ["alarm"],
        "min_severity": Severity.MAJOR.value,
        "categories": list(CATEGORIES),
        # One OOB switch failure reads as one incident on the console because
        # correlation collapsed twenty symptoms into it. Exporting the symptoms
        # would un-collapse exactly what that work achieved.
        "exclude_symptoms": True,
        # Planned work does not page anyone, and it does not open tickets
        # either. The window already has a change record.
        "exclude_shelved": True,
        # A condition must have been open this long before its first ticket.
        # The alarm engine's own dwell already damps the metric; this damps the
        # ones that arrive pre-formed - traps and equipment alarm points - which
        # have no dwell of their own.
        "dwell_s": 300,
    },

    # ------------------------------------------------------------ mapping
    "priority_map": {
        Severity.CRITICAL.value: "Highest",
        Severity.MAJOR.value: "High",
        Severity.MINOR.value: "Medium",
        Severity.WARNING.value: "Low",
        Severity.INFO.value: "Lowest",
    },
    "labels": {"prefix": "dcim", "site": True, "room": True, "category": True},

    # ------------------------------------------ operations alerts (phase 6)
    #
    # P1-P5 and nothing else. The Operations API does not REJECT an
    # unrecognised priority - it silently substitutes P3 - so a map that let
    # "CRITICAL" through would page the estate's worst faults at the same
    # urgency as its mildest and nothing would ever say so. `validate`
    # enforces the range for the same reason.
    "ops_priority_map": {
        Severity.CRITICAL.value: "P1",
        Severity.MAJOR.value: "P2",
        Severity.MINOR.value: "P3",
        Severity.WARNING.value: "P4",
        Severity.INFO.value: "P5",
    },
    #: Teams to page. Empty leaves routing to the team's own rules in JSM,
    #: which is usually what an on-call setup already encodes.
    "ops_responders": [],
    # customfield_NNNNN ids, resolved by the connection test and cached here.
    # Empty means "do not send that field", which is the right behaviour on a
    # project where nobody created it.
    "fields": {"device": "", "location": "", "first_seen": "",
               "occurrences": "", "metric": ""},
    # Off by default: a component that does not exist in the target project
    # fails the whole create, and there is no way to know from here whether it
    # exists until the connection test says so.
    "components_enabled": False,
    # Which renderer the service desk API gets for `description`.
    #
    # `/rest/api/3/issue` demands ADF for that field; `POST
    # /rest/servicedeskapi/request` has historically taken a plain STRING for
    # the same one, and the sources disagree about whether that is still true.
    # Rather than guess for every tenant, the mapper builds one document and
    # renders it either way, the connection test posts a scratch request to
    # find out which this tenant accepts, and the answer is stored here.
    "jsm_description_adf": False,

    # ---------------------------------------------------------- lifecycle
    "reopen_window_h": 24,
    "wont_reopen_resolutions": ["Won't Fix", "Duplicate", "Declined"],
    # What a clear does to the ticket. `comment` is the default because a
    # fault clearing is not the same as the work being finished - somebody
    # still has to replace the fan that the CRAH is now running without.
    "close_on_clear": "comment",
    "clear_transition": "Done",
    # THE EXCEPTION TO "only the poll clears", and it is off.
    #
    # A Jira transition to Done normally ACKNOWLEDGES an alarm and never
    # clears it, because an engineer who fixed the symptom - or who closed the
    # ticket because the part arrives Thursday - would otherwise silence a
    # live fault from outside this platform entirely. If the condition really
    # is over, the next poll clears it within one interval.
    #
    # Turning this on is defensible for conditions with no polled backstop: a
    # manual-source alarm that nothing will ever come back to clear. It is
    # indefensible for everything else, so the settings page labels it "this
    # trusts Jira over the plane", which is exactly what it does.
    "allow_clear_on_transition": False,

    # ------------------------------------------------------ storm control
    "suppress_window_s": 600,
    "storm_threshold": 20,
    "storm_window_s": 300,

    # -------------------------------------------------------- dispatching
    # Well under Jira's ~100 RPS burst limit. We would rather be slow than
    # throttled: a 429 costs a round trip AND a Retry-After wait, so pacing is
    # cheaper than discovering the limit.
    "rate_limit_rps": 5,
    "max_attempts": 8,
}

#: Keys that may appear inside `policy`, `labels`, `fields` and `priority_map`.
_NESTED = ("policy", "labels", "fields", "priority_map")

CLOSE_ON_CLEAR = ("comment", "transition", "none")


class IntegrationConfigError(ValueError):
    """A rejected setting, with a message written for the operator."""


def resolved(config: dict[str, Any] | None) -> dict[str, Any]:
    """The stored document over the defaults, one level deep into each section.

    Deep-merged per section rather than replaced, so setting one policy clause
    does not silently drop the other five.
    """
    out = copy.deepcopy(DEFAULTS)
    for key, value in (config or {}).items():
        if key in _NESTED and isinstance(value, dict):
            out[key].update(value)
        elif key in DEFAULTS:
            out[key] = value
    return out


def validate(config: dict[str, Any]) -> dict[str, Any]:
    """Check a whole document and return the cleaned, still-sparse version.

    Sparse in, sparse out: this never fills in defaults. Doing so here is the
    mistake that freezes them.
    """
    if not isinstance(config, dict):
        raise IntegrationConfigError("configuration must be a set of settings")

    out: dict[str, Any] = {}
    for key, raw in config.items():
        if key not in DEFAULTS:
            raise IntegrationConfigError(f"'{key}' is not an integration setting")
        if raw is None:
            continue                       # falls back to the default
        out[key] = _section(key, raw)
    return out


def _section(key: str, raw: Any) -> Any:
    if key == "policy":
        return _policy(_dict(key, raw))
    if key == "labels":
        return _labels(_dict(key, raw))
    if key == "fields":
        return _fields(_dict(key, raw))
    if key == "priority_map":
        return _priority_map(_dict(key, raw))
    if key == "ops_priority_map":
        return _ops_priority_map(_dict(key, raw))

    if key in ("project_key", "issue_type", "service_desk_id", "request_type_id",
               "change_issue_type", "change_request_type_id",
               "clear_transition"):
        return _text(key, raw, max_len=100)
    if key == "close_on_clear":
        if raw not in CLOSE_ON_CLEAR:
            raise IntegrationConfigError(
                f"close_on_clear is one of {', '.join(CLOSE_ON_CLEAR)}")
        return raw
    if key == "ops_responders":
        return [_text("responder", v, max_len=100) for v in _list(key, raw)]
    if key in ("wont_reopen_resolutions", "change_approved_statuses",
               "change_declined_statuses"):
        return [_text("status", v, max_len=100) for v in _list(key, raw)]
    if key in ("components_enabled", "jsm_description_adf",
               "allow_clear_on_transition"):
        return _bool(key, raw)

    return _int(key, raw, **_BOUNDS[key])


#: Every numeric setting's range, and why the ceiling is where it is.
_BOUNDS: dict[str, dict[str, int]] = {
    # A day is the longest a reopen makes sense for: past that the condition
    # has recurred rather than continued, and it deserves its own ticket with
    # its own start time. A week would silently attach a Tuesday fault to a
    # Monday ticket somebody had already reported on.
    "reopen_window_h": {"lo": 0, "hi": 168},
    "suppress_window_s": {"lo": 0, "hi": 86_400},
    # Below 2 the breaker fires on ordinary correlated pairs.
    "storm_threshold": {"lo": 2, "hi": 1000},
    "storm_window_s": {"lo": 60, "hi": 3600},
    # Jira's documented burst limit is around 100 RPS and its hourly point
    # quota is tenant-shaped; 20 is already far more than any sane estate
    # produces and leaves the quota to the humans using Jira.
    "rate_limit_rps": {"lo": 1, "hi": 20},
    "max_attempts": {"lo": 1, "hi": 20},
}


def _policy(raw: dict[str, Any]) -> dict[str, Any]:
    allowed = set(DEFAULTS["policy"])
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in allowed:
            raise IntegrationConfigError(f"'{key}' is not a policy clause")
        if value is None:
            continue
        if key == "response_classes":
            clean[key] = _subset(key, value, RESPONSE_CLASSES)
        elif key == "categories":
            clean[key] = _subset(key, value, CATEGORIES)
        elif key == "min_severity":
            if value not in SEVERITY_RANK or value == Severity.CLEAR:
                raise IntegrationConfigError(
                    "min_severity is one of CRITICAL, MAJOR, MINOR, WARNING, INFO")
            clean[key] = value
        elif key == "dwell_s":
            clean[key] = _int(key, value, lo=0, hi=86_400)
        else:
            clean[key] = _bool(key, value)
    if clean.get("response_classes") == [] or clean.get("categories") == []:
        # An empty list reads as "none of them", which silently disables the
        # integration while the UI still shows it enabled. Whoever wants that
        # turns the integration off.
        raise IntegrationConfigError(
            "a policy that matches nothing would disable ticketing silently; "
            "turn the integration off instead")
    return clean


def _labels(raw: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in DEFAULTS["labels"]:
            raise IntegrationConfigError(f"'{key}' is not a label setting")
        if key == "prefix":
            text = _text(key, value, max_len=30)
            # Jira labels cannot contain whitespace; one sneaking in makes
            # every create fail with a field error that names the label and
            # not the setting behind it.
            if any(c.isspace() for c in text):
                raise IntegrationConfigError("the label prefix cannot contain spaces")
            clean[key] = text
        else:
            clean[key] = _bool(key, value)
    return clean


def _fields(raw: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in DEFAULTS["fields"]:
            raise IntegrationConfigError(f"'{key}' is not a mappable field")
        text = _text(key, value, max_len=60)
        if text and not text.startswith("customfield_"):
            raise IntegrationConfigError(
                f"{key} must be a Jira custom field id like customfield_10101")
        clean[key] = text
    return clean


def _ops_priority_map(raw: dict[str, Any]) -> dict[str, Any]:
    """P1-P5, enforced.

    The one validation in this file that prevents a SILENT failure rather
    than a loud one: the Operations API substitutes P3 for anything it does
    not recognise, so a typo here does not error, it just quietly flattens
    the estate's urgency.
    """
    from app.integrations.ops.target import PRIORITIES
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in DEFAULTS["ops_priority_map"]:
            raise IntegrationConfigError(f"'{key}' is not a severity")
        # Generous, so the P1-P5 check below is the one that reports - a
        # length complaint naming the SEVERITY while objecting to the VALUE
        # sends somebody looking in the wrong half of the setting.
        text = _text(key, value, max_len=60).upper()
        if text not in PRIORITIES:
            raise IntegrationConfigError(
                f"{key} must map to one of {', '.join(PRIORITIES)} - the "
                f"Operations API silently substitutes P3 for anything else")
        clean[key] = text
    return clean


def _priority_map(raw: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in DEFAULTS["priority_map"]:
            raise IntegrationConfigError(f"'{key}' is not a severity")
        clean[key] = _text(key, value, max_len=60)
    return clean


# ------------------------------------------------------------------ scalars

def _dict(label: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise IntegrationConfigError(f"{label} takes a set of settings")
    return raw


def _list(label: str, raw: Any) -> list[Any]:
    if not isinstance(raw, list):
        raise IntegrationConfigError(f"{label} takes a list")
    return raw


def _subset(label: str, raw: Any, allowed: tuple[str, ...]) -> list[str]:
    values = _list(label, raw)
    unknown = sorted({str(v) for v in values} - set(allowed))
    if unknown:
        raise IntegrationConfigError(
            f"unknown {label}: {', '.join(unknown)}")
    # Ordered by the canonical order, not by what the caller sent, so two
    # equivalent documents compare equal and a save does not look like a change.
    return [a for a in allowed if a in {str(v) for v in values}]


def _text(label: str, raw: Any, *, max_len: int) -> str:
    if not isinstance(raw, str):
        raise IntegrationConfigError(f"{label} must be text")
    text = raw.strip()
    if len(text) > max_len:
        raise IntegrationConfigError(f"{label} is longer than {max_len} characters")
    return text


def _bool(label: str, raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    raise IntegrationConfigError(f"{label} is on or off")


def _int(label: str, raw: Any, *, lo: int, hi: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        try:
            raw = int(str(raw))
        except (TypeError, ValueError):
            raise IntegrationConfigError(
                f"{label} must be a whole number") from None
    if not (lo <= raw <= hi):
        raise IntegrationConfigError(f"{label} must be between {lo} and {hi}")
    return int(raw)
