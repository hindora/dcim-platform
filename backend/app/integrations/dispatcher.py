"""Draining the outbox: claim, deliver, record, and decide what happens next.

Runs inside the ingest worker, on the same timer pattern as the staleness
sweep, because that process already owns a supervised lifecycle, a session
factory and a Redis handle. It does NOT run in the API: an API process that
blocks on somebody else's service desk is an API process that stops answering.

FOUR PROPERTIES, EACH BOUGHT DELIBERATELY:

1. **Two workers are safe.** The claim is `FOR UPDATE SKIP LOCKED`, so the
   second worker takes different rows rather than blocking on the first's and
   then re-processing them.

2. **Per-fingerprint order is preserved.** Rows are grouped by fingerprint and
   each group is processed serially, in id order. Without this a clear could
   be delivered before the raise it clears - the ticket would be closed and
   then a second one opened for a fault that was already over.

3. **The HTTP call is never inside the transaction that produced the alarm.**
   The outbox exists precisely so that the slow, failing, rate-limited part
   happens later and somewhere else.

4. **A failure is scheduled, not retried in place.** `attempts` lives in the
   row, so a crash between attempts does not reset the count and a row that
   reliably kills its worker still walks to the dead state instead of looping
   forever.

THE STORM BRAKE is the one piece with no equivalent in any product surveyed.
When the policy would open more tickets in a window than a human could read,
opening them is not help. New creates are held and one platform alarm says so;
follow-ups on tickets that already exist continue, because closing them is
still useful. A held create re-checks whether its alarm is still open before
it finally fires, so the brake is a brake and not a delay line.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import unit_of_work
from app.integrations import backoff, inbox
from app.integrations.change import PENDING
from app.integrations.config import resolved
from app.integrations.jira.client import JiraClient, JiraError
from app.integrations.ratelimit import Limiter
from app.repositories import integrations as repo
from app.repositories import maintenance as maintenance_repo
from app.services import integrations as service

log = get_logger("integrations.dispatcher")

#: How often the worker drains the outbox. Not configurable: it is a floor on
#: how late a ticket can be, and five seconds is already far below the time a
#: human takes to notice an alarm. Shorter would mean more empty queries on an
#: estate that is, most of the time, fine.
DISPATCH_EVERY_S = 5.0

#: Rows per pass, per worker. Small on purpose - a pass holds its claims for
#: as long as the HTTP calls take, and a large batch is a long lock plus a
#: long time before a second worker can help.
BATCH = 25

#: A claim older than this belonged to a worker that died. Comfortably longer
#: than a batch of 25 calls could take at the default 5 RPS.
STALE_CLAIM_S = 300

#: A create that has been waiting longer than this re-checks whether its alarm
#: is still open. See `_should_still_create`.
RECHECK_AFTER_S = 300

#: Inbound rows per pass. Larger than the outbound batch because applying one
#: is a handful of indexed statements against our own database rather than a
#: paced HTTP call to somebody else's.
INBOUND_BATCH = 100

#: Attempts before an inbound row is parked. Low: an inbound failure is a bug
#: in our own reader, not a transient, and repeating it produces identical
#: tracebacks rather than a different answer.
INBOUND_ATTEMPTS = 3

#: How often to look for registrations about to lapse. Daily would do - the
#: deadline is 30 days out - but an hour costs one partial-index read and
#: means a worker that was down at the wrong moment still catches it.
WEBHOOK_SWEEP_S = 3600.0

#: Refresh this far ahead of expiry, so a failed attempt has a week of
#: retries left rather than one.
WEBHOOK_REFRESH_DAYS = 7

#: Undo the claim's increment for a row that was HELD rather than attempted.
#: Nothing failed, so a held row must not walk towards the dead state for
#: having waited - otherwise a long storm retires the very tickets it was
#: protecting the desk from.
_DECREMENT_ATTEMPT = text(
    "UPDATE integration_outbox SET attempts = GREATEST(0, attempts - 1) "
    "WHERE id = :id")


class Dispatcher:
    """One per ingest worker. Holds a client per integration, and their state."""

    def __init__(self, consumer: str) -> None:
        self.consumer = consumer
        self._last_run = float("-inf")
        self._last_reap = float("-inf")
        self._last_webhook_sweep = float("-inf")
        self._clients: dict[str, tuple[int, JiraClient, Any]] = {}
        self._limiters: dict[str, Limiter] = {}
        # Creation timestamps per integration, for the storm brake. A deque
        # rather than a counter so the window slides instead of resetting -
        # a counter that resets on the minute lets twice the threshold
        # through across a boundary.
        self._creates: dict[str, deque[float]] = defaultdict(deque)

    async def aclose(self) -> None:
        for _, client, _ in self._clients.values():
            await client.aclose()
        self._clients.clear()

    # ------------------------------------------------------------- timing

    async def maybe_run(self) -> None:
        """Called from the ingest worker's tick. Cheap when there is nothing."""
        now = time.monotonic()
        if now - self._last_run < DISPATCH_EVERY_S:
            return
        self._last_run = now
        try:
            await self.run_once()
            await self.drain_inbound()
            await self._maybe_refresh_webhooks()
        except Exception as exc:
            # A failed pass must never stop telemetry ingestion. It runs again
            # in five seconds and the rows are still there.
            log.error("integration dispatch failed", error=str(exc), exc_info=True)

    async def run_once(self) -> int:
        async with unit_of_work() as session:
            if time.monotonic() - self._last_reap > STALE_CLAIM_S:
                self._last_reap = time.monotonic()
                released = await repo.release_stale_claims(session, STALE_CLAIM_S)
                released += await repo.release_stale_inbound(session, STALE_CLAIM_S)
                if released:
                    log.info("released stale integration claims", rows=released)
            rows = await repo.claim(session, consumer=self.consumer, limit=BATCH)
        if not rows:
            return 0

        # Grouped, then each group serially. Two rows for one fingerprint in
        # one batch means the second depends on what the first did to the
        # ticket - most obviously a raise followed by its own clear.
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[(row["integration_id"], row["fingerprint"])].append(row)

        delivered = 0
        for (integration_id, print_), group in groups.items():
            delivered += await self._deliver_group(integration_id, print_, group)
        return delivered

    # ---------------------------------------------------------- delivery

    async def _deliver_group(self, integration_id: str, print_: str,
                             group: list[dict[str, Any]]) -> int:
        async with unit_of_work() as session:
            integration = await repo.get_integration(session, integration_id)
            if integration is None or not integration["enabled"]:
                # Disabled or deleted between the claim and now. The rows are
                # retired rather than left pending, or they would be claimed
                # again every five seconds forever.
                for row in group:
                    await repo.mark_done(session, row["id"])
                return 0
            target = await self._target_for(session, integration)
            # An Operations integration keeps no link table: the alias IS the
            # key and the API holds it, so there is nothing to look up and
            # nothing to write back.
            links = ({} if integration["kind"] == "jsm_ops"
                     else await repo.links_for(session, integration_id, [print_]))

        link = links.get(print_)
        cfg = resolved(integration.get("config"))
        delivered = 0

        for row in group:
            try:
                link = await self._deliver_one(target, cfg, integration, row,
                                               print_, link)
                delivered += 1
            except JiraError as exc:
                await self._schedule_retry(integration, row, exc, cfg)
                # Stop this fingerprint here. The rows behind this one depend
                # on the ticket state this one would have produced, and
                # delivering them out of order is worse than delivering them
                # late.
                break
            except Exception as exc:
                # A bug in the mapper, not a failure of Jira. Retrying it eight
                # times produces eight identical tracebacks; it goes dead with
                # the message so somebody can read it.
                log.error("integration row failed unexpectedly", row=row["id"],
                          error=str(exc), exc_info=True)
                async with unit_of_work() as session:
                    await repo.mark_dead(session, row["id"],
                                         error=f"{type(exc).__name__}: {exc}")
                break
        return delivered

    async def _deliver_one(self, target: Any, cfg: dict[str, Any],
                           integration: dict[str, Any], row: dict[str, Any],
                           print_: str, link: dict[str, Any] | None
                           ) -> dict[str, Any] | None:
        alarm = row["payload"]
        kind = row["kind"]

        # A window's messages take their own path. Not a special case bolted
        # onto the alarm one: a change request has no fingerprint to dedup on,
        # no reopen window and nothing to brake, and forcing it through those
        # five branches would mean five places where "this does not apply to a
        # window" has to be remembered.
        if kind.startswith("window_"):
            await self._deliver_window(target, integration, row)
            return link

        if integration["kind"] == "jsm_ops":
            await self._deliver_alert(target, integration, row, print_)
            return link
        creating = link is None or link.get("closed_at") is not None

        if creating and kind != "alarm_cleared":
            keep = await self._should_still_create(row)
            if not keep:
                async with unit_of_work() as session:
                    await repo.mark_done(session, row["id"])
                log.info("held ticket dropped: the condition cleared while "
                         "it waited", fingerprint=print_, row=row["id"])
                return link
            if self._storm_tripped(integration, cfg):
                await self._hold(integration, row, cfg)
                raise _HeldError(print_)

        update = await target.handle(kind=kind, alarm=alarm, print_=print_,
                                     link=link, attempts=row["attempts"])

        async with unit_of_work() as session:
            if update is not None:
                stored = await repo.upsert_link(
                    session, integration_id=integration["id"],
                    fingerprint=print_, issue_key=update.issue_key,
                    issue_id=update.issue_id, alarm_id=row.get("alarm_id"),
                    device_id=alarm.get("device_id"),
                    # The condition's identity, so the INBOUND path can find
                    # the alarm that is open now rather than the row this
                    # ticket happened to be opened for.
                    alarm_type=alarm.get("alarm_type"),
                    instance=alarm.get("instance") or "",
                    status=update.status,
                    status_category=update.status_category,
                    resolution=update.resolution, closed_at=update.closed_at,
                    wont_reopen=update.wont_reopen, reopened=update.reopened)
                link = stored
                if creating:
                    self._note_create(integration["id"])
            await repo.mark_done(session, row["id"])
        return link

    async def _deliver_alert(self, target: Any, integration: dict[str, Any],
                             row: dict[str, Any], print_: str) -> None:
        """An alert, which has no state of its own to persist.

        The whole of the issue path's bookkeeping - the link row, the reopen
        window, the recovery search - exists because a Jira issue has no
        notion of "the same problem again". An Operations alert does: the
        alias is the dedup key and the API owns it. So this is one call and a
        `mark_done`.

        The storm brake still applies, and matters more here than anywhere
        else: the issue path fills a queue, this one wakes people up.
        """
        cfg = resolved(integration.get("config"))
        if row["kind"] != "alarm_cleared" and self._storm_tripped(integration, cfg):
            await self._hold(integration, row, cfg)
            raise _HeldError(print_)

        update = await target.handle(kind=row["kind"], alarm=row["payload"],
                                     print_=print_, attempts=row["attempts"])
        if update is not None and update.action == "created":
            self._note_create(integration["id"])
        async with unit_of_work() as session:
            await repo.mark_done(session, row["id"])

    async def _deliver_window(self, target: Any,
                              integration: dict[str, Any],
                              row: dict[str, Any]) -> None:
        payload = row["payload"]
        window = payload.get("window") or {}
        if not hasattr(target, "handle_window"):
            # A change request is not an alert. An Operations integration has
            # nowhere to put one, and queueing it against a paging tier would
            # wake somebody up about paperwork.
            log.info("change request skipped: this integration pages rather "
                     "than tickets", integration=integration["name"])
            async with unit_of_work() as session:
                await repo.mark_done(session, row["id"])
            return
        update = await target.handle_window(
            kind=row["kind"], window=window, payload=payload,
            attempts=row["attempts"])
        async with unit_of_work() as session:
            if update is not None:
                await maintenance_repo.set_change(
                    session, window["id"], issue_key=update.issue_key,
                    integration_id=integration["id"],
                    # Opening the request does not approve it. A window that
                    # requires approval stays pending until Jira says
                    # otherwise; one that does not never had an approval state
                    # and must not acquire one here, or the console would show
                    # it waiting for a decision nobody was asked to make.
                    approval_state=(PENDING if window.get("require_approval")
                                    else window.get("jira_approval_state")))
            await repo.mark_done(session, row["id"])

    async def _should_still_create(self, row: dict[str, Any]) -> bool:
        """Is a delayed create still worth making?

        Only asked of rows that have waited - held by the brake, or stuck
        behind a long backoff. A fresh row is acted on as recorded, because a
        condition that raises and clears within one pass is a flap and the
        alarm engine's dwell is where that belongs.
        """
        created = row.get("created_at")
        if not isinstance(created, datetime):
            return True
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if (datetime.now(UTC) - created).total_seconds() < RECHECK_AFTER_S:
            return True
        alarm_id = row.get("alarm_id")
        if not alarm_id:
            return True
        async with unit_of_work() as session:
            return alarm_id in await repo.open_alarm_ids(session, [alarm_id])

    # ------------------------------------------------------- storm brake

    def _storm_tripped(self, integration: dict[str, Any],
                       cfg: dict[str, Any]) -> bool:
        window = float(cfg.get("storm_window_s") or 0)
        threshold = int(cfg.get("storm_threshold") or 0)
        if not window or not threshold:
            return False
        recent = self._creates[integration["id"]]
        cutoff = time.monotonic() - window
        while recent and recent[0] < cutoff:
            recent.popleft()
        return len(recent) >= threshold

    def _note_create(self, integration_id: str) -> None:
        self._creates[integration_id].append(time.monotonic())

    async def _hold(self, integration: dict[str, Any], row: dict[str, Any],
                    cfg: dict[str, Any]) -> None:
        window = int(cfg.get("storm_window_s") or 300)
        when = backoff.next_attempt_at(1, retry_after=float(window))
        async with unit_of_work() as session:
            # `attempts` was already incremented by the claim, and a held row
            # must not walk towards the dead state for waiting: nothing failed.
            await session.execute(_DECREMENT_ATTEMPT, {"id": row["id"]})
            await repo.mark_retry(
                session, row["id"], not_before=when,
                error=(f"held: more than {cfg.get('storm_threshold')} tickets "
                       f"in {window}s"))
        log.warning("storm brake engaged; new tickets held",
                    integration=integration["name"],
                    threshold=cfg.get("storm_threshold"), window_s=window)

    # ------------------------------------------------------------ retries

    async def _schedule_retry(self, integration: dict[str, Any],
                              row: dict[str, Any], exc: JiraError,
                              cfg: dict[str, Any]) -> None:
        if isinstance(exc, _HeldError):
            return                                  # already scheduled by _hold
        max_attempts = int(cfg.get("max_attempts") or 8)
        message = str(exc)

        if not exc.retryable:
            # A 400 is the payload being wrong - a custom field that is not on
            # this project's create screen, a component nobody created, a
            # renamed priority. Eight identical attempts spend eight requests
            # of the tenant's hourly quota to learn the same thing and bury
            # the one useful error under seven copies.
            async with unit_of_work() as session:
                await repo.mark_dead(session, row["id"], error=message)
            log.error("integration row rejected", row=row["id"],
                      integration=integration["name"], status=exc.status,
                      error=message)
            return

        if row["attempts"] >= max_attempts:
            async with unit_of_work() as session:
                await repo.mark_dead(session, row["id"],
                                     error=f"gave up after {row['attempts']} "
                                           f"attempts: {message}")
            log.error("integration row gave up", row=row["id"],
                      integration=integration["name"], attempts=row["attempts"],
                      error=message)
            return

        when = backoff.next_attempt_at(row["attempts"],
                                       retry_after=exc.retry_after)
        async with unit_of_work() as session:
            await repo.mark_retry(session, row["id"], not_before=when,
                                  error=message)
        log.info("integration row will be retried", row=row["id"],
                 attempts=row["attempts"], at=when.isoformat(),
                 status=exc.status)


    # ---------------------------------------------------------- inbound

    async def drain_inbound(self) -> int:
        """Apply what Jira has told us.

        Separate from the outbound pass and run on the same tick. The two are
        deliberately not interleaved: an outbound failure retries for half an
        hour, and letting that hold up an acknowledgement an engineer made
        five minutes ago would make the DCIM look stale in the one place an
        operator checks after closing a ticket.
        """
        async with unit_of_work() as session:
            rows = await repo.claim_inbound(session, consumer=self.consumer,
                                            limit=INBOUND_BATCH)
        if not rows:
            return 0

        applied = 0
        for row in rows:
            try:
                async with unit_of_work() as session:
                    integration = await repo.get_integration(
                        session, row["integration_id"])
                    if integration is None:
                        await repo.mark_inbound(session, row["id"],
                                                state="done")
                        continue
                    result = await inbox.apply(session, integration,
                                               row["payload"])
                    await repo.mark_inbound(session, row["id"], state="done")
                applied += 1
                if result.get("action") not in ("ignore", "unlinked"):
                    log.info("jira event applied", **result)
            except Exception as exc:
                # One malformed payload must not stop the queue behind it.
                # Inbound rows are independent of each other in a way outbound
                # rows are not - they are already ordered by id and each one
                # names its own issue - so the queue carries on and the row is
                # kept for somebody to look at.
                await self._fail_inbound(row, exc)
        return applied

    async def _fail_inbound(self, row: dict[str, Any], exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"
        dead = row["attempts"] >= INBOUND_ATTEMPTS
        async with unit_of_work() as session:
            await repo.mark_inbound(session, row["id"],
                                    state="dead" if dead else "pending",
                                    error=message)
        log.error("jira event failed", row=row["id"], event=row.get("event"),
                  issue=row.get("issue_key"), attempts=row["attempts"],
                  dead=dead, error=message, exc_info=True)

    # ------------------------------------------------- webhook lifetime

    async def _maybe_refresh_webhooks(self) -> None:
        """Keep dynamic registrations alive.

        A Jira Cloud webhook registered through the REST API expires 30 days
        after it is created. Nothing fails when it does: no error, no bounce,
        no log line anywhere. The tickets simply stop answering back a month
        after somebody set the integration up, and the first anybody knows is
        an alarm that stayed ACTIVE after the ticket was closed.

        So it is swept on a slow timer, and a registration that is close to
        lapsing and cannot be refreshed becomes a platform alarm rather than a
        silence. Hand-registered webhooks - which is what Cloud installs using
        an API token actually have - never expire and never appear here.
        """
        now = time.monotonic()
        if now - self._last_webhook_sweep < WEBHOOK_SWEEP_S:
            return
        self._last_webhook_sweep = now
        try:
            async with unit_of_work() as session:
                due = await repo.webhooks_needing_refresh(
                    session, within_days=WEBHOOK_REFRESH_DAYS)
            for row in due:
                await self._refresh_one(row["id"], row["name"])
        except Exception as exc:
            log.error("webhook refresh sweep failed", error=str(exc),
                      exc_info=True)

    async def _refresh_one(self, integration_id: str, name: str) -> None:
        try:
            async with unit_of_work() as session:
                integration = await repo.get_integration(session, integration_id)
                if integration is None:
                    return
                expires = await service.refresh_webhook(session, integration)
            if expires:
                log.info("jira webhook registration extended",
                         integration=name, until=expires.isoformat())
        except Exception as exc:
            # Not fatal and not silent. The platform alarm raised from
            # `webhook_expires_at` is what an operator sees; this line is what
            # tells them why.
            log.error("could not extend a jira webhook registration",
                      integration=name, error=str(exc))

    # ------------------------------------------------------------ clients

    async def _target_for(self, session: AsyncSession,
                          integration: dict[str, Any]) -> Any:
        """One client per integration, rebuilt when its configuration moves.

        Keyed on `version` so a credential rotation or a project change takes
        effect on the next pass without a worker restart - and so that a
        cached client cannot keep using a credential the operator has revoked.
        """
        cached = self._clients.get(integration["id"])
        if cached and cached[0] == integration["version"]:
            return cached[2]
        if cached:
            await cached[1].aclose()

        limiter = self._limiters.get(integration["id"])
        if limiter is None:
            cfg = resolved(integration.get("config"))
            limiter = Limiter(float(cfg.get("rate_limit_rps") or 5))
            self._limiters[integration["id"]] = limiter

        # The credential is loaded and decrypted by the SERVICE, never here.
        # One decrypting path for the whole application is the property
        # `test_secret_access` protects, and a second copy in a worker would
        # be just as hard to audit as one in a handler.
        client, target = await service.build_target(session, integration,
                                                    limiter=limiter)
        self._clients[integration["id"]] = (integration["version"], client, target)
        return target


class _HeldError(JiraError):
    """Not a failure: the storm brake declined to open another ticket."""

    def __init__(self, fingerprint: str) -> None:
        super().__init__(f"held by the storm brake ({fingerprint})", status=429)
