"""Routing rules: who is emailed about which alarms.

A rule is a recipient list plus the same policy clauses a ticketing integration
uses (`app.integrations.policy`), so "which conditions count" means one thing
across the platform. The defaults differ, and on purpose: a ticket waits five
minutes for a pre-formed condition to prove itself; a page for a critical alarm
should not, so a rule's delay starts at zero.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alert_taxonomy import CATEGORIES
from app.integrations.config import IntegrationConfigError, validate
from app.notifications.smtp import valid_email

MAX_RECIPIENTS = 20

DEFAULT_POLICY: dict[str, Any] = {
    "response_classes": ["alarm"],
    "min_severity": "MAJOR",
    "categories": list(CATEGORIES),
    "exclude_symptoms": True,
    "exclude_shelved": True,
    # Here: how long a condition must stay active before its first email.
    "dwell_s": 0,
    "alarm_types": [],
}


class RuleError(ValueError):
    """A rule that cannot be saved, with a sentence a form can show."""


def resolved_policy(stored: dict[str, Any] | None) -> dict[str, Any]:
    out = copy.deepcopy(DEFAULT_POLICY)
    out.update(stored or {})
    return out


def clean(raw: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate a create (current None) or a partial update."""
    out: dict[str, Any] = {}

    if "name" in raw or current is None:
        name = " ".join(str(raw.get("name") or "").split())
        if not name:
            raise RuleError("give the rule a name, such as “NOC on-call”")
        if len(name) > 80:
            raise RuleError("the name is longer than 80 characters")
        out["name"] = name

    if "recipients" in raw or current is None:
        out["recipients"] = clean_recipients(raw.get("recipients"))

    if "policy" in raw:
        try:
            cleaned = validate({"policy": raw.get("policy") or {}}).get("policy", {})
        except IntegrationConfigError as exc:
            raise RuleError(str(exc)) from exc
        if cleaned.get("dwell_s", 0) > 3600:
            raise RuleError("the delay before the first email is at most an hour")
        out["policy"] = cleaned
    elif current is None:
        out["policy"] = {}

    for key in ("enabled", "notify_on_clear", "notify_on_escalation"):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise RuleError(f"{key.replace('_', ' ')} must be true or false")
            out[key] = raw[key]

    if "max_per_hour" in raw:
        try:
            n = int(raw["max_per_hour"])
        except (TypeError, ValueError) as exc:
            raise RuleError("the hourly limit must be a number") from exc
        if not 1 <= n <= 500:
            raise RuleError("the hourly limit is between 1 and 500 emails")
        out["max_per_hour"] = n

    unknown = set(raw) - {
        "name",
        "recipients",
        "policy",
        "enabled",
        "notify_on_clear",
        "notify_on_escalation",
        "max_per_hour",
    }
    if unknown:
        raise RuleError(f"'{sorted(unknown)[0]}' is not a rule setting")
    return out


def clean_recipients(raw: Any) -> list[str]:
    """A list, or one string split on commas, semicolons and line breaks."""
    if isinstance(raw, str):
        items = raw.replace(";", ",").replace("\n", ",").split(",")
    elif isinstance(raw, list):
        items = [str(x) for x in raw]
    else:
        items = []
    seen: list[str] = []
    for item in (i.strip() for i in items):
        if not item:
            continue
        if not valid_email(item):
            raise RuleError(f"“{item}” is not an email address")
        if item.lower() not in (s.lower() for s in seen):
            seen.append(item)
    if not seen:
        raise RuleError("add at least one recipient")
    if len(seen) > MAX_RECIPIENTS:
        raise RuleError(
            f"a rule sends to at most {MAX_RECIPIENTS} addresses; use a distribution list for more"
        )
    return seen


# ------------------------------------------------------------------ storage

_SELECT = """
    SELECT r.id::text, r.name, r.enabled, r.recipients, r.policy,
           r.notify_on_clear, r.notify_on_escalation, r.max_per_hour,
           r.created_at, r.updated_at, r.updated_by,
           (SELECT max(o.sent_at) FROM notification_outbox o
             WHERE o.rule_id = r.id AND o.status = 'sent') AS last_sent_at,
           (SELECT count(*) FROM notification_outbox o
             WHERE o.rule_id = r.id AND o.status = 'sent'
               AND o.sent_at > now() - interval '24 hours') AS sent_24h
      FROM notification_rule r
"""


def _view(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["recipients"] = list(out["recipients"] or [])
    out["policy"] = resolved_policy(out.get("policy"))
    return out


async def list_rules(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text(_SELECT + " ORDER BY r.name"))).mappings().all()
    return [_view(r) for r in rows]


async def get_rule(session: AsyncSession, rule_id: str) -> dict[str, Any] | None:
    row = (
        (await session.execute(text(_SELECT + " WHERE r.id = CAST(:id AS uuid)"), {"id": rule_id}))
        .mappings()
        .first()
    )
    return _view(row) if row else None


async def raw_rule(session: AsyncSession, rule_id: str) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text("SELECT * FROM notification_rule WHERE id = CAST(:id AS uuid)"),
                {"id": rule_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def name_taken(session: AsyncSession, name: str, except_id: str | None = None) -> bool:
    return (
        await session.execute(
            text("""
        SELECT 1 FROM notification_rule
         WHERE lower(name) = lower(:n)
           AND (CAST(:id AS uuid) IS NULL OR id <> CAST(:id AS uuid))
    """),
            {"n": name, "id": except_id},
        )
    ).first() is not None


async def create(session: AsyncSession, fields: dict[str, Any], actor: str) -> str:
    row = (
        await session.execute(
            text("""
        INSERT INTO notification_rule (name, enabled, recipients, policy, notify_on_clear,
                                       notify_on_escalation, max_per_hour, updated_by)
        VALUES (:name, :enabled, :recipients, CAST(:policy AS jsonb), :clear, :esc, :maxh, :actor)
        RETURNING id::text
    """),
            {
                "name": fields["name"],
                "enabled": fields.get("enabled", True),
                "recipients": fields["recipients"],
                "policy": json.dumps(fields.get("policy") or {}),
                "clear": fields.get("notify_on_clear", True),
                "esc": fields.get("notify_on_escalation", True),
                "maxh": fields.get("max_per_hour", 30),
                "actor": actor,
            },
        )
    ).first()
    return row[0]


async def update(session: AsyncSession, rule_id: str, fields: dict[str, Any], actor: str) -> None:
    sets, params = [], {"id": rule_id, "actor": actor}
    for key, value in fields.items():
        if key == "policy":
            sets.append("policy = CAST(:policy AS jsonb)")
            params["policy"] = json.dumps(value)
        else:
            sets.append(f"{key} = :{key}")
            params[key] = value
    sets += ["updated_at = now()", "updated_by = :actor"]
    await session.execute(
        text(f"UPDATE notification_rule SET {', '.join(sets)} WHERE id = CAST(:id AS uuid)"), params
    )


async def delete(session: AsyncSession, rule_id: str) -> bool:
    res = await session.execute(
        text("DELETE FROM notification_rule WHERE id = CAST(:id AS uuid)"), {"id": rule_id}
    )
    return (res.rowcount or 0) > 0


async def deliveries(
    session: AsyncSession, *, limit: int, offset: int, status: str | None = None
) -> tuple[list[dict[str, Any]], int]:
    where = "WHERE (CAST(:status AS text) IS NULL OR o.status = :status)"
    params = {"status": status, "limit": limit, "offset": offset}
    total = (
        await session.execute(text(f"SELECT count(*) FROM notification_outbox o {where}"), params)
    ).scalar_one()
    rows = (
        (
            await session.execute(
                text(f"""
        SELECT o.id, o.rule_id::text, r.name AS rule_name, o.kind, o.status, o.attempts,
               o.subject, o.note, o.created_at, o.sent_at, o.next_attempt_at,
               o.alarm_id::text AS alarm_id,
               o.payload->>'severity' AS severity, o.payload->>'device_name' AS device_name
          FROM notification_outbox o
          JOIN notification_rule r ON r.id = o.rule_id
          {where}
         ORDER BY o.id DESC
         LIMIT :limit OFFSET :offset
    """),
                params,
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows], int(total)
