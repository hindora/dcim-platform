"""Which conditions earn a ticket.

THE DEFAULT THIS MODULE EXISTS TO AVOID is "every alarm becomes an issue". It
is one line of code and it is wrong here for three reasons that are specific to
this platform rather than to ticketing in general:

* The taxonomy already separates a condition that needs a response now
  (`response_class = alarm`) from one that belongs to whoever schedules the
  work (`alert`). Mirroring everything throws that away at the door.
* Correlation already collapses a cascade: one OOB switch failure is one
  incident with twenty suppressed symptoms, not twenty-one rows. An exporter
  that ignores `is_symptom` un-collapses precisely what that work achieved,
  and does it on the service desk where nobody can see the root.
* A maintenance window already shelves planned work so it does not page. It
  must not open tickets either; the window has a change record of its own.

Every clause below reads a column that already exists and is already indexed,
so the policy is cheap enough to evaluate on the ingest path.

WHAT THIS IS NOT. It is not a gate on follow-ups. Once a ticket exists for a
fingerprint, every later action on that condition goes out regardless of what
the policy says - you must be able to close the ticket you opened, and a
policy edit between the raise and the clear must not strand an open issue.
That rule lives in `outbox.enqueue`, which is the only caller; this module
answers the narrower question "would this condition open a ticket".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.integrations.config import SEVERITY_RANK

#: Returned by `explain` when nothing objected.
MATCH = ""


def matches(alarm: dict[str, Any], policy: dict[str, Any], *,
            now: datetime | None = None) -> bool:
    """Would this condition open a ticket?"""
    return explain(alarm, policy, now=now) == MATCH


def explain(alarm: dict[str, Any], policy: dict[str, Any], *,
            now: datetime | None = None) -> str:
    """The first clause that refused, in words, or `MATCH`.

    A predicate that only returns False is unusable in a settings page: the
    operator's question is never "did it match" but "why did nothing come
    through", and answering that by reading the policy back to them and
    letting them guess is how support tickets are made.
    """
    response_class = alarm.get("response_class")
    allowed = policy.get("response_classes") or []
    if response_class not in allowed:
        return (f"response class {response_class!r} is not in "
                f"{', '.join(allowed) or 'nothing'}")

    severity = str(alarm.get("severity") or "")
    floor = str(policy.get("min_severity") or "")
    # Worst-first ranking, so "at least MAJOR" is rank <= rank(MAJOR). An
    # unknown severity ranks last and is therefore excluded, which is the safe
    # direction: a severity this platform does not recognise is not something
    # to page a service desk about.
    if SEVERITY_RANK.get(severity, 99) > SEVERITY_RANK.get(floor, -1):
        return f"severity {severity} is below {floor}"

    categories = policy.get("categories") or []
    if alarm.get("category") not in categories:
        return f"category {alarm.get('category')!r} is not ticketed"

    if policy.get("exclude_symptoms", True) and alarm.get("is_symptom"):
        return "it is a symptom of another alarm, which carries the ticket"

    if policy.get("exclude_shelved", True) and alarm.get("shelved"):
        # Two things shelve, and an engineer reading "no ticket was opened"
        # needs to know which: work in progress on a live machine is a very
        # different answer from a machine nobody has accepted yet.
        return _SHELVE_REASONS.get(alarm.get("shelved_reason"), "it is shelved")

    dwell = int(policy.get("dwell_s") or 0)
    if dwell:
        age = _age_s(alarm.get("first_seen"), now)
        # None means we cannot tell how old it is. Treat that as "not yet" -
        # the next action on the same condition carries a usable first_seen,
        # and opening a ticket on a timestamp we could not read is the worse
        # of the two mistakes.
        if age is None:
            return "its start time could not be read"
        if age < dwell:
            return f"it has been open {int(age)}s, less than the {dwell}s dwell"

    return MATCH


#: Why a shelved alarm was passed over, in words a ticket comment can carry.
#: Keyed by `alarm.shelved_reason`; an unknown key falls back to the bare fact,
#: so a reason added to the database without being added here degrades to a
#: less specific sentence rather than to a KeyError.
_SHELVE_REASONS = {
    "maintenance_window": "it is shelved by a maintenance window",
    "not_commissioned": ("it is shelved: the device is installed but not yet "
                         "accepted into service"),
}


def _age_s(first_seen: Any, now: datetime | None) -> float | None:
    if first_seen is None:
        return None
    if isinstance(first_seen, str):
        try:
            first_seen = datetime.fromisoformat(first_seen)
        except ValueError:
            return None
    if not isinstance(first_seen, datetime):
        return None
    # Rows from asyncpg are timezone-aware; a hand-built one in a test may not
    # be, and subtracting a naive from an aware datetime raises rather than
    # being wrong quietly. Assume UTC, which is what the column stores.
    if first_seen.tzinfo is None:
        first_seen = first_seen.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - first_seen).total_seconds()
