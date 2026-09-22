"""Turning alarm actions into intents to ticket, inside the caller's
transaction.

Called from the ingest worker at the three places alarm actions are produced,
each of them already inside `unit_of_work()`. That placement is the whole
point and it is easy to undo by accident: move this call below the `async
with` and the alarm commits while the intent does not, which is a ticket that
is never opened for a fault that definitely happened.

THE CHEAP PATH MATTERS. This runs on every telemetry batch of a 1400-endpoint
estate, and on the overwhelming majority of ticks the answer is "nothing to
do". So the order is: no enabled integrations, return; no actions, return; a
severity pre-filter on the thin dict we already have, return; and only then
one query to widen what survived. On a healthy estate the first three cost
nothing and the fourth never runs.

FOLLOW-UPS ARE NOT SUBJECT TO THE POLICY. Once a ticket exists for a
fingerprint, every later action on that condition goes out - clears,
escalations, acknowledgements - whatever the policy has since been edited to
say. Otherwise tightening the policy on a Tuesday afternoon strands every
issue opened that morning in the open state with nothing coming to close it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.integrations import fingerprint as fp
from app.integrations import policy as policy_mod
from app.integrations.config import SEVERITY_RANK, resolved
from app.repositories import integrations as repo

log = get_logger("integrations.outbox")

#: Map from the fan-out's action kinds to the outbox's.
#:
#: `alarm_updated` splits: an escalation is worth telling a service desk about
#: and a mere re-occurrence is not, because the second one happens every poll
#: while a condition persists and would be a write per poll per alarm. The
#: `change` field the alarm repository stamps is what tells them apart.
_KINDS = {
    "alarm_created": "alarm_raised",
    "alarm_cleared": "alarm_cleared",
}

#: The widest severity any policy can ask for. Anything milder cannot match
#: any policy, so it is dropped before the widening query runs.
_LOOSEST_RANK = SEVERITY_RANK["INFO"]


async def enqueue_actions(session: AsyncSession, actions: list[Any]) -> int:
    """Record what should be ticketed from one batch of alarm actions.

    ``actions`` are `app.alarms.service.AlarmAction`. Returns how many intents
    were written, which is almost always zero.
    """
    if not actions:
        return 0
    integrations = await repo.active(session)
    if not integrations:
        return 0

    # Pre-filter on the thin dict, before any query. A clear is always a
    # candidate - it may be closing a ticket opened when the policy was
    # looser - and a raise has to clear the loosest severity bar any policy
    # could set.
    candidates: list[tuple[str, dict[str, Any]]] = []
    for action in actions:
        alarm = action.alarm
        kind = _kind_of(action)
        if kind is None:
            continue
        if kind != "alarm_cleared":
            rank = SEVERITY_RANK.get(str(alarm.get("severity") or ""), 99)
            if rank > _LOOSEST_RANK:
                continue
        if not alarm.get("id"):
            continue
        candidates.append((kind, alarm))
    if not candidates:
        return 0

    wide = await repo.alarms_for_export(
        session, sorted({a["id"] for _, a in candidates}))

    written = 0
    for integration in integrations:
        rows = await _rows_for(session, integration, candidates, wide)
        written += await repo.enqueue(session, rows)
    return written


async def _rows_for(session: AsyncSession, integration: dict[str, Any],
                    candidates: list[tuple[str, dict[str, Any]]],
                    wide: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    cfg = resolved(integration.get("config"))
    policy = cfg["policy"]

    prints: dict[str, str] = {}
    for _, alarm in candidates:
        row = wide.get(alarm["id"])
        if row is not None:
            prints[alarm["id"]] = fp.of_alarm(row)
    if not prints:
        return []

    already_open = await repo.open_fingerprints(
        session, integration["id"], sorted(set(prints.values())))

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for kind, alarm in candidates:
        row = wide.get(alarm["id"])
        if row is None:
            # The alarm vanished between the action and this query - a manual
            # clear racing a sweep, or a device deleted mid-tick. Nothing to
            # describe, and inventing a payload from the thin dict would put a
            # ticket on the desk with no location on it.
            continue
        print_ = prints[alarm["id"]]
        followup = print_ in already_open

        if not followup:
            if kind == "alarm_cleared":
                # A clear with no open ticket is the normal case on a healthy
                # estate: most conditions never earned one.
                continue
            why = policy_mod.explain(row, policy)
            if why:
                log.debug("condition not ticketed", fingerprint=print_,
                          alarm_type=row.get("alarm_type"), reason=why)
                continue

        # One row per (fingerprint, kind) per batch. A tick that raises and
        # re-raises the same condition would otherwise write two identical
        # intents and pay for two Jira writes to say one thing.
        key = (print_, kind)
        if key in seen:
            continue
        seen.add(key)

        rows.append({
            "integration_id": integration["id"],
            "kind": kind,
            "fingerprint": print_,
            "alarm_id": row["id"],
            # Frozen. The alarm row mutates under us - severity escalates,
            # occurrence_count climbs, the message is rewritten by the next
            # detector to speak - and a comment that says "this was MAJOR at
            # 09:14" must still say that when it is delivered an hour later.
            "payload": row,
        })
    return rows


def _kind_of(action: Any) -> str | None:
    kind = getattr(action, "kind", None)
    if kind in _KINDS:
        return _KINDS[kind]
    if kind == "alarm_updated":
        change = (getattr(action, "alarm", None) or {}).get("change")
        # `escalated` only. A `touched` update is the same condition still
        # being true, which is what an occurrence count is for, and a
        # `deescalated` one does not warrant interrupting a service desk.
        return "alarm_escalated" if change == "escalated" else None
    return None
