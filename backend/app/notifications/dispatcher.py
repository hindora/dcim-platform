"""Delivering queued emails: claim, decide, send, record.

Runs inside the ingest worker on a timer, like the ticketing dispatcher, and
for the same reason: that process already has a supervised lifecycle and a
session factory, and the API must never block on somebody's mail relay.

ORDER WITHOUT A LOCK. Only the OLDEST unfinished row of each (rule, condition)
can be claimed - the claim query refuses a row with an older pending or sending
sibling. So a clear can never overtake the raise it closes, even with two
workers claiming at once, and no pass-wide lock is needed. The cost is that a
raise and its clear take two passes, five seconds apart.

DECISIONS AT SEND TIME, from the database as it is now:

* a raise held for its delay is dropped if the alarm cleared meanwhile;
* a clear is dropped if no raise or escalation was actually sent for the
  condition since its last clear - nobody was told it started;
* a rule over its hourly limit has further raises recorded as suppressed
  rather than sent. Clears still go, so nobody is left believing a condition
  is still active.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any

from sqlalchemy import text

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import unit_of_work
from app.integrations import backoff
from app.notifications import message as msg_mod
from app.notifications import smtp
from app.notifications.rules import resolved_policy
from app.services import instance_settings

log = get_logger("notifications.dispatcher")

DISPATCH_EVERY_S = 5.0
BATCH = 50
MAX_ATTEMPTS = 8
STALE_CLAIM_S = 120
KEEP_DAYS = 30
PRUNE_EVERY_S = 3600.0

_CLAIM = text("""
    UPDATE notification_outbox o
       SET status = 'sending', claimed_by = :me, claimed_at = now()
     WHERE o.id IN (
        SELECT c.id FROM notification_outbox c
         WHERE c.status = 'pending' AND c.next_attempt_at <= now()
           AND NOT EXISTS (
               SELECT 1 FROM notification_outbox e
                WHERE e.rule_id = c.rule_id AND e.fingerprint = c.fingerprint
                  AND e.id < c.id AND e.status IN ('pending', 'sending'))
         ORDER BY c.id
         LIMIT :limit
         FOR UPDATE SKIP LOCKED)
    RETURNING o.id, o.rule_id::text AS rule_id, o.kind, o.fingerprint,
              o.alarm_id::text AS alarm_id, o.payload, o.attempts
""")


class Notifier:
    """One per ingest worker."""

    def __init__(self, consumer: str) -> None:
        self.consumer = consumer
        self._last_run = float("-inf")
        self._last_prune = float("-inf")

    async def maybe_run(self) -> None:
        now = time.monotonic()
        if now - self._last_run < DISPATCH_EVERY_S:
            return
        self._last_run = now
        try:
            await self.run_once()
            if now - self._last_prune > PRUNE_EVERY_S:
                self._last_prune = now
                await self._prune()
        except Exception as exc:
            # Never stops ingest. The rows are still there in five seconds.
            log.error("notification dispatch failed", error=str(exc), exc_info=True)

    async def run_once(self) -> int:
        async with unit_of_work() as session:
            await session.execute(
                text("""
                UPDATE notification_outbox SET status = 'pending', claimed_by = NULL
                 WHERE status = 'sending' AND claimed_at < now() - make_interval(secs => :s)
            """),
                {"s": STALE_CLAIM_S},
            )
            rows = [
                dict(r)
                for r in (await session.execute(_CLAIM, {"me": self.consumer, "limit": BATCH}))
                .mappings()
                .all()
            ]
        if not rows:
            return 0

        async with unit_of_work() as session:
            cfg = await smtp.load(session, with_password=True)
            rules = {
                r["id"]: r
                for r in (
                    await session.execute(
                        text("""
                SELECT id::text, name, enabled, recipients, policy, notify_on_clear,
                       max_per_hour,
                       (SELECT count(*) FROM notification_outbox o
                         WHERE o.rule_id = r.id AND o.status = 'sent'
                           AND o.kind <> 'alarm_cleared'
                           AND o.sent_at > now() - interval '1 hour') AS sent_last_hour
                  FROM notification_rule r WHERE id = ANY(CAST(:ids AS uuid[]))
            """),
                        {"ids": sorted({r["rule_id"] for r in rows})},
                    )
                )
                .mappings()
                .all()
            }
            org = (await instance_settings.load(session)).org_name
        org_name, _ = instance_settings.resolve(org, get_settings().org_name)

        if not cfg.enabled:
            await self._finish_all(
                rows, "failed", "email is turned off in Settings > Notifications"
            )
            return 0

        to_send: list[tuple[dict[str, Any], EmailMessage, str]] = []
        outcomes: list[tuple[dict[str, Any], str, str | None, str | None]] = []
        budget = {
            rid: int(r["max_per_hour"]) - int(r["sent_last_hour"]) for rid, r in rules.items()
        }

        for row in rows:
            rule = rules.get(row["rule_id"])
            if rule is None or not rule["enabled"]:
                outcomes.append(
                    (row, "skipped", "the rule was turned off before it was sent", None)
                )
                continue
            reason = await self._why_not(row)
            if reason:
                outcomes.append((row, "skipped", reason, None))
                continue
            if row["kind"] != "alarm_cleared":
                if budget.get(rule["id"], 0) <= 0:
                    outcomes.append(
                        (
                            row,
                            "suppressed",
                            f"over the rule's limit of {rule['max_per_hour']} emails an hour",
                            None,
                        )
                    )
                    continue
                budget[rule["id"]] -= 1
            message, subject = await self._build(row, rule, cfg, org_name)
            to_send.append((row, message, subject))

        if to_send:
            results = await asyncio.to_thread(smtp.send_batch, cfg, [m for _, m, _ in to_send])
            for (row, message, subject), err in zip(to_send, results, strict=True):
                if err is None:
                    outcomes.append((row, "sent", None, message["Message-ID"]))
                    row["_subject"] = subject
                else:
                    row["_subject"] = subject
                    outcomes.append(
                        (row, "retry" if not err.permanent else "failed", str(err), None)
                    )

        sent = 0
        async with unit_of_work() as session:
            for row, status, note, message_id in outcomes:
                if status == "retry":
                    attempts = row["attempts"] + 1
                    if attempts >= MAX_ATTEMPTS:
                        status, note = "failed", f"gave up after {attempts} attempts: {note}"
                    else:
                        await session.execute(
                            text("""
                            UPDATE notification_outbox
                               SET status = 'pending', attempts = :a, note = :n,
                                   subject = COALESCE(:s, subject),
                                   next_attempt_at = :due, claimed_by = NULL
                             WHERE id = :id
                        """),
                            {
                                "a": attempts,
                                "n": note,
                                "s": row.get("_subject"),
                                "due": backoff.next_attempt_at(attempts),
                                "id": row["id"],
                            },
                        )
                        continue
                await session.execute(
                    text("""
                    UPDATE notification_outbox
                       SET status = :st, note = :n, subject = COALESCE(:s, subject),
                           message_id = :mid, attempts = attempts + :tried,
                           sent_at = CASE WHEN :st = 'sent' THEN now() ELSE sent_at END,
                           claimed_by = NULL
                     WHERE id = :id
                """),
                    {
                        "st": status,
                        "n": note,
                        "s": row.get("_subject"),
                        "mid": message_id,
                        "tried": 1 if "_subject" in row else 0,
                        "id": row["id"],
                    },
                )
                sent += status == "sent"
        if outcomes:
            log.info("notifications processed", sent=sent, rows=len(outcomes))
        return sent

    async def _why_not(self, row: dict[str, Any]) -> str | None:
        async with unit_of_work() as session:
            if row["kind"] == "alarm_cleared":
                prior = (
                    await session.execute(
                        text("""
                    SELECT 1 FROM notification_outbox o
                     WHERE o.rule_id = CAST(:rule AS uuid) AND o.fingerprint = :fp
                       AND o.kind IN ('alarm_raised', 'alarm_escalated') AND o.status = 'sent'
                       AND o.id < :id
                       AND o.id > COALESCE((
                           SELECT max(c.id) FROM notification_outbox c
                            WHERE c.rule_id = o.rule_id AND c.fingerprint = o.fingerprint
                              AND c.kind = 'alarm_cleared' AND c.id < :id), 0)
                     LIMIT 1
                """),
                        {"rule": row["rule_id"], "fp": row["fingerprint"], "id": row["id"]},
                    )
                ).first()
                return None if prior else "nobody was emailed that it started"
            if row["kind"] == "alarm_raised" and row.get("alarm_id"):
                state = (
                    await session.execute(
                        text("SELECT state::text FROM alarm WHERE id = CAST(:id AS uuid)"),
                        {"id": row["alarm_id"]},
                    )
                ).scalar_one_or_none()
                if state == "CLEARED":
                    return "it cleared before the delay ended"
        return None

    async def _build(
        self, row: dict[str, Any], rule: dict[str, Any], cfg: smtp.SmtpConfig, org_name: str
    ) -> tuple[EmailMessage, str]:
        alarm = row["payload"]
        subject = msg_mod.subject(row["kind"], alarm)
        policy = resolved_policy(rule["policy"])
        body = msg_mod.body(
            row["kind"],
            alarm,
            rule_name=rule["name"],
            rule_summary=msg_mod.rule_summary(policy, notify_on_clear=rule["notify_on_clear"]),
            org_name=org_name,
            link_base_url=cfg.link_base_url,
        )
        message = smtp.base_message(cfg, org_name, list(rule["recipients"]), subject, body)
        domain = cfg.from_address.rpartition("@")[2] or "dcim.local"
        message["Message-ID"] = make_msgid(idstring=f"dcim.{row['id']}", domain=domain)
        message["X-DCIM-Severity"] = str(alarm.get("severity") or "")
        message["X-DCIM-Event"] = msg_mod.VERB.get(row["kind"], "updated")
        if alarm.get("id"):
            message["X-DCIM-Alarm-Id"] = str(alarm["id"])
        if row["kind"] != "alarm_raised":
            async with unit_of_work() as session:
                first = (
                    await session.execute(
                        text("""
                    SELECT message_id FROM notification_outbox
                     WHERE rule_id = CAST(:rule AS uuid) AND fingerprint = :fp
                       AND kind = 'alarm_raised' AND status = 'sent' AND id < :id
                     ORDER BY id DESC LIMIT 1
                """),
                        {"rule": row["rule_id"], "fp": row["fingerprint"], "id": row["id"]},
                    )
                ).scalar_one_or_none()
            if first:
                message["In-Reply-To"] = first
                message["References"] = first
        return message, subject

    async def _finish_all(self, rows: list[dict[str, Any]], status: str, note: str) -> None:
        async with unit_of_work() as session:
            await session.execute(
                text("""
                UPDATE notification_outbox SET status = :st, note = :n, claimed_by = NULL
                 WHERE id = ANY(:ids)
            """),
                {"st": status, "n": note, "ids": [r["id"] for r in rows]},
            )

    async def _prune(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=KEEP_DAYS)
        async with unit_of_work() as session:
            res = await session.execute(
                text("""
                DELETE FROM notification_outbox o
                 WHERE o.created_at < :cut AND o.status NOT IN ('pending', 'sending')
                   -- A condition still open keeps its history: its clear looks
                   -- for the raise that was sent, however long ago.
                   AND NOT EXISTS (SELECT 1 FROM notification_open n
                                    WHERE n.rule_id = o.rule_id
                                      AND n.fingerprint = o.fingerprint)
            """),
                {"cut": cutoff},
            )
        if res.rowcount:
            log.info("notification log pruned", rows=res.rowcount)
