"""Turning alarm actions into emails to send, inside the caller's transaction.

Same placement rule as the ticketing outbox (`app.integrations.outbox`): this
runs inside the ingest worker's `unit_of_work()`, so the alarm and the intent
to email about it commit together or not at all.

THE CHEAP PATH. This runs on every telemetry batch. No actions: return. No
enabled rule, or email turned off: one indexed query and return. Only then the
widening query for the alarms that survived.

FOLLOW-UPS FOLLOW THE FIRST EMAIL. `notification_open` holds the conditions a
rule has emailed about (or is about to). A clear or an escalation of one of
those goes out whatever the rule's policy says now - somebody was told the
condition started and must be told it ended - and a clear of a condition never
emailed about goes nowhere.

THE DELAY is not a filter here, unlike the ticketing policy's dwell: the email
is queued now and held until `first_seen + delay`, and the dispatcher drops it
if the alarm cleared in the meantime. A filter would never fire at all, since
nothing re-offers a raise once its moment has passed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.integrations import fingerprint as fp
from app.integrations import policy as policy_mod
from app.integrations.outbox import _kind_of
from app.notifications.rules import resolved_policy
from app.repositories import integrations as integrations_repo

log = get_logger("notifications.outbox")


async def active_rules(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                text("""
        SELECT r.id::text, r.name, r.policy, r.notify_on_clear, r.notify_on_escalation
          FROM notification_rule r
         WHERE r.enabled
           AND EXISTS (SELECT 1 FROM smtp_setting s WHERE s.id = 1 AND s.enabled)
    """)
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def enqueue_actions(session: AsyncSession, actions: list[Any]) -> int:
    """Queue the emails one batch of alarm actions calls for. Returns how many."""
    if not actions:
        return 0
    candidates = [
        (k, a.alarm)
        for a in actions
        if (k := _kind_of(a)) is not None and (a.alarm or {}).get("id")
    ]
    if not candidates:
        return 0
    rules = await active_rules(session)
    if not rules:
        return 0

    wide = await integrations_repo.alarms_for_export(
        session, sorted({a["id"] for _, a in candidates})
    )
    prints = {aid: fp.of_alarm(row) for aid, row in wide.items()}
    if not prints:
        return 0

    open_rows = (
        await session.execute(
            text("""
        SELECT rule_id::text, fingerprint FROM notification_open
         WHERE rule_id = ANY(CAST(:rules AS uuid[])) AND fingerprint = ANY(:fps)
    """),
            {"rules": [r["id"] for r in rules], "fps": sorted(set(prints.values()))},
        )
    ).all()
    is_open = {(r[0], r[1]) for r in open_rows}

    now = datetime.now(UTC)
    queued: list[dict[str, Any]] = []
    opened: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for rule in rules:
        policy = resolved_policy(rule["policy"])
        # The delay is applied by holding the email, not by the policy.
        gate = {**policy, "dwell_s": 0}
        delay = int(policy.get("dwell_s") or 0)
        for kind, thin in candidates:
            row = wide.get(thin["id"])
            if row is None:
                continue
            print_ = prints[thin["id"]]
            key = (rule["id"], print_)
            send_kind: str | None = None
            due = now

            if kind == "alarm_cleared":
                if key in is_open:
                    is_open.discard(key)
                    closed.append({"rule": rule["id"], "fp": print_})
                    if rule["notify_on_clear"]:
                        send_kind = kind
            elif key in is_open:
                if kind == "alarm_escalated" and rule["notify_on_escalation"]:
                    send_kind = kind
                # A re-raise of a condition already emailed about says nothing new.
            else:
                why = policy_mod.explain(row, gate, now=now)
                if why:
                    continue
                # An escalation that first crosses this rule's floor is, to this
                # rule's recipients, the start of the condition.
                send_kind = "alarm_raised"
                is_open.add(key)
                opened.append({"rule": rule["id"], "fp": print_, "alarm": row["id"]})
                if delay:
                    started = row.get("first_seen")
                    if isinstance(started, datetime):
                        started = started if started.tzinfo else started.replace(tzinfo=UTC)
                        due = max(now, started + timedelta(seconds=delay))
                    else:
                        due = now + timedelta(seconds=delay)

            if send_kind is None or (rule["id"], print_, send_kind) in seen:
                continue
            seen.add((rule["id"], print_, send_kind))
            queued.append(
                {
                    "rule": rule["id"],
                    "kind": send_kind,
                    "fp": print_,
                    "alarm": row["id"],
                    # Frozen, like the ticketing payload: an email that says "MAJOR
                    # at 09:14" must still say so if the relay is down until 10:00.
                    "payload": json.dumps(row, default=str),
                    "due": due,
                }
            )

    if closed:
        await session.execute(
            text("""
            DELETE FROM notification_open
             WHERE rule_id = CAST(:rule AS uuid) AND fingerprint = :fp
        """),
            closed,
        )
    if opened:
        await session.execute(
            text("""
            INSERT INTO notification_open (rule_id, fingerprint, alarm_id)
            VALUES (CAST(:rule AS uuid), :fp, CAST(:alarm AS uuid))
            ON CONFLICT (rule_id, fingerprint) DO NOTHING
        """),
            opened,
        )
    if queued:
        await session.execute(
            text("""
            INSERT INTO notification_outbox
                   (rule_id, kind, fingerprint, alarm_id, payload, next_attempt_at)
            VALUES (CAST(:rule AS uuid), :kind, :fp, CAST(:alarm AS uuid),
                    CAST(:payload AS jsonb), :due)
        """),
            queued,
        )
        log.info("notifications queued", count=len(queued))
    return len(queued)
