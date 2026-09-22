"""Applying what Jira said, to the alarm it was about.

**THE RULE THIS MODULE EXISTS TO ENFORCE: a closed ticket is not a cleared
fault.** Jira "Done" moves the alarm to ACKNOWLEDGED. It never clears it. Only
the poll clears, which is the same standing rule the trap path lives by, and
it is not pedantry - an engineer who fixed the symptom, or who closed the
ticket because the work is scheduled for Thursday, would otherwise silence a
live fault from outside the DCIM entirely. If the condition really is over,
the next poll clears it within one interval and the outbound side comments on
the ticket saying so.

There is one exception and it is off by default: `allow_clear_on_transition`,
for alarm types with no polled backstop. The settings page labels it "this
trusts Jira over the plane", which is exactly what it does.

**WHICH alarm.** Not the one the link was opened for. A condition clears at
02:00 and raises again at 06:00 as a new row with a new uuid; the ticket is
about the CONDITION. So the link carries `(device_id, alarm_type, instance)` -
the tuple `alarm_active_key` indexes - and resolution goes through that, with
`alarm_id` as the fallback for links written before migration 0070.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.alarms import correlation, link_correlation
from app.core import audit
from app.core.logging import get_logger
from app.integrations import change
from app.integrations.config import resolved
from app.integrations.jira import webhook
from app.repositories import alarms as alarm_repo
from app.repositories import integrations as repo
from app.repositories import maintenance as maintenance_repo

log = get_logger("integrations.inbox")


async def apply(session: AsyncSession, integration: dict[str, Any],
                payload: dict[str, Any]) -> dict[str, Any]:
    """Act on one inbound event. Returns what happened, for the log.

    Participates in the caller's transaction and never commits: the alarm
    change, its history row and its audit row land together or not at all.
    """
    cfg = resolved(integration.get("config"))
    declined = tuple(r.strip().casefold()
                     for r in cfg.get("wont_reopen_resolutions") or ())
    decision = webhook.interpret(payload, declined=declined or
                                 webhook.DEFAULT_DECLINED)

    if decision.action == "ignore" or not decision.issue_key:
        return {"action": "ignore"}

    link = await repo.link_by_issue(session, integration["id"],
                                    decision.issue_key)
    if link is None:
        # Not a condition's ticket. It may still be a WINDOW's change request,
        # which is the other thing this platform opens - and the one whose
        # answer decides whether an engineer gets through the door tonight.
        applied = await _window_event(session, integration, cfg, decision)
        if applied is not None:
            return applied
        # A ticket in the watched project that this platform did not open.
        # Normal - the project is shared with humans - and deliberately not an
        # error, but counted, because a webhook filter that is too wide is
        # otherwise invisible.
        log.debug("inbound event for an unknown issue",
                  issue=decision.issue_key, action=decision.action)
        return {"action": "unlinked", "issue_key": decision.issue_key}

    handler = _HANDLERS.get(decision.action)
    if handler is None:                                  # pragma: no cover
        return {"action": "ignore"}
    return await handler(session, integration, cfg, link, decision)


async def _window_event(session: AsyncSession, integration: dict[str, Any],
                        cfg: dict[str, Any], decision: webhook.Decision
                        ) -> dict[str, Any] | None:
    """A change request moving, and what it says about its window.

    Returns None when the issue is not a window's, so the caller can fall
    through to "we did not open this".

    The status NAME is read here rather than the category, and that is the one
    place in this package where that is right: a change workflow's categories
    are useless - "Awaiting approval", "Approved" and "Implementing" are all
    `indeterminate` - so the names are configuration with defaults that match
    Jira's own change template.
    """
    window = await maintenance_repo.window_by_issue(
        session, integration["id"], decision.issue_key)
    if window is None:
        return None

    verdict = change.decide(
        decision.status,
        approved=tuple(cfg.get("change_approved_statuses") or ()),
        declined=tuple(cfg.get("change_declined_statuses") or ()))
    if verdict is None:
        # A real transition that says nothing about approval - Draft to
        # Awaiting CAB, say. Recorded and otherwise ignored: treating every
        # unrecognised status as a refusal would cancel windows for paperwork.
        log.debug("change request moved, approval unchanged",
                  issue=decision.issue_key, status=decision.status)
        return {"action": "change_noted", "issue_key": decision.issue_key,
                "status": decision.status}

    from app.services import maintenance as maintenance_service
    moved = await maintenance_service.apply_approval(
        session, window["id"], verdict)
    await audit.record(
        session, actor=decision.actor,
        action=f"maintenance.change.{verdict}",
        target_type="maintenance_window", target_id=window["id"],
        before={"approval": window.get("jira_approval_state"),
                "status": window.get("status")},
        after={"approval": verdict, "issue_key": decision.issue_key,
               "cancelled": bool(moved and moved.get("cancelled"))})
    log.info("change request decided", issue=decision.issue_key,
             window=window["id"], verdict=verdict, actor=decision.actor)
    return {"action": f"change_{verdict}", "issue_key": decision.issue_key,
            "window": window["id"],
            "cancelled": bool(moved and moved.get("cancelled"))}


# --------------------------------------------------------------- handlers

async def _acknowledge(session: AsyncSession, integration: dict[str, Any],
                       cfg: dict[str, Any], link: dict[str, Any],
                       decision: webhook.Decision) -> dict[str, Any]:
    await repo.record_inbound(
        session, link["fingerprint"], status=decision.status,
        status_category=decision.status_category,
        resolution=decision.resolution, closed=True)

    alarm = await _current_alarm(session, link)
    if alarm is None:
        # The ticket was closed for a condition that is already gone. Nothing
        # to acknowledge, and that is the happy path: the fault cleared and
        # somebody tidied up after it.
        await _audit(session, integration, decision, link,
                     after={"outcome": "no open alarm"})
        return {"action": "acknowledge", "issue_key": decision.issue_key,
                "alarm": None}

    if cfg.get("allow_clear_on_transition"):
        return await _clear(session, integration, cfg, link, decision, alarm)

    row = await alarm_repo.acknowledge(
        session, alarm["id"], decision.actor,
        f"Resolved in {decision.issue_key}"
        + (f" as {decision.resolution}" if decision.resolution else ""))
    if row is None:
        # Already acknowledged, or already cleared. Two people reaching for
        # one alarm is normal and is not an error; nothing moved, and the
        # audit row below still records that Jira asked.
        await _audit(session, integration, decision, link,
                     after={"outcome": "alarm was not ACTIVE"})
        return {"action": "acknowledge", "issue_key": decision.issue_key,
                "alarm": alarm["id"], "changed": False}

    await alarm_repo.record_history(
        session, alarm_id=row["id"], device_id=row["device_id"],
        action="acknowledged", severity=row["severity"], actor=decision.actor,
        detail={"issue_key": decision.issue_key,
                "resolution": decision.resolution})
    await _audit(session, integration, decision, link,
                 before={"state": "ACTIVE"},
                 after={"state": "ACKNOWLEDGED", "alarm_id": row["id"]})
    log.info("alarm acknowledged from jira", alarm=row["id"],
             issue=decision.issue_key, actor=decision.actor)
    return {"action": "acknowledge", "issue_key": decision.issue_key,
            "alarm": row["id"], "changed": True}


async def _clear(session: AsyncSession, integration: dict[str, Any],
                 cfg: dict[str, Any], link: dict[str, Any],
                 decision: webhook.Decision,
                 alarm: dict[str, Any]) -> dict[str, Any]:
    """The opt-in exception. Off by default, and loud when it is on.

    Reachable only for an install that has ticked "trust Jira over the plane",
    which is defensible for alarm types with no polled backstop - a
    manual-source condition nothing will ever come back to clear - and
    indefensible for everything else.
    """
    row = await alarm_repo.manual_clear(session, alarm["id"], decision.actor)
    if row is None:
        return {"action": "clear", "issue_key": decision.issue_key,
                "changed": False}
    await alarm_repo.record_history(
        session, alarm_id=row["id"], device_id=row["device_id"],
        action="cleared", severity=row["severity"], actor=decision.actor,
        detail={"issue_key": decision.issue_key})
    # A cleared ROOT has to let go of what it was explaining, or its symptoms
    # stay suppressed behind an alarm that no longer exists - invisible on the
    # console and unclearable. Every other clear path does this; this one is
    # new and would have been the next to forget.
    for symptom in await correlation.release_symptoms(session, row["id"]):
        await alarm_repo.record_history(
            session, alarm_id=symptom["id"], device_id=symptom["device_id"],
            action="released", severity=symptom["severity"],
            actor=decision.actor, detail={"root": row["id"]})
    await link_correlation.refresh_link_state(
        session, device_id=row["device_id"], instance=link.get("instance") or "")
    await alarm_repo.refresh_device_alarm_state(session, [row["device_id"]])
    await _audit(session, integration, decision, link,
                 before={"state": alarm.get("state")},
                 after={"state": "CLEARED", "alarm_id": row["id"],
                        "via": "allow_clear_on_transition"})
    log.warning("alarm CLEARED from jira", alarm=row["id"],
                issue=decision.issue_key, actor=decision.actor,
                detail="allow_clear_on_transition is enabled")
    return {"action": "clear", "issue_key": decision.issue_key,
            "alarm": row["id"], "changed": True}


async def _declined(session: AsyncSession, integration: dict[str, Any],
                    cfg: dict[str, Any], link: dict[str, Any],
                    decision: webhook.Decision) -> dict[str, Any]:
    """Won't Fix, Duplicate, Declined.

    The LINK closes, so a recurrence opens a fresh ticket rather than
    reopening one a human explicitly declined. The ALARM is untouched: nobody
    said the condition was dealt with, and acknowledging it would take a live
    fault off the console on the strength of somebody saying "not this one".
    """
    await repo.record_inbound(
        session, link["fingerprint"], status=decision.status,
        status_category=decision.status_category,
        resolution=decision.resolution, closed=True, wont_reopen=True)
    await _audit(session, integration, decision, link,
                 after={"outcome": "declined; the alarm is untouched",
                        "resolution": decision.resolution})
    return {"action": "declined", "issue_key": decision.issue_key}


async def _moved(session: AsyncSession, integration: dict[str, Any],
                 cfg: dict[str, Any], link: dict[str, Any],
                 decision: webhook.Decision) -> dict[str, Any]:
    """A status change to something that is not Done.

    Reopen or progress, decided HERE because only the link knows where the
    issue was. Both are recorded; neither touches the alarm - an alarm is
    acknowledged by a decision, not by somebody dragging a card.
    """
    was_closed = link.get("closed_at") is not None
    await repo.record_inbound(
        session, link["fingerprint"], status=decision.status,
        status_category=decision.status_category, resolution=None,
        closed=False, reopened=was_closed)

    alarm = await _current_alarm(session, link)
    if alarm is not None:
        await alarm_repo.record_history(
            session, alarm_id=alarm["id"], device_id=alarm["device_id"],
            action="jira_reopened" if was_closed else "jira_in_progress",
            severity=alarm.get("severity"), actor=decision.actor,
            detail={"issue_key": decision.issue_key, "status": decision.status})
    if was_closed:
        await _audit(session, integration, decision, link,
                     before={"closed": True},
                     after={"closed": False, "status": decision.status})
    return {"action": "reopened" if was_closed else "progress",
            "issue_key": decision.issue_key}


async def _comment(session: AsyncSession, integration: dict[str, Any],
                   cfg: dict[str, Any], link: dict[str, Any],
                   decision: webhook.Decision) -> dict[str, Any]:
    """What an engineer wrote, onto the alarm's own history.

    The one place somebody records what they actually found, and without this
    it lives only in Jira - so the DCIM's history of a recurring fault reads
    as a list of raises with no explanation attached to any of them.
    """
    alarm = await _current_alarm(session, link)
    if alarm is None:
        return {"action": "comment", "issue_key": decision.issue_key,
                "alarm": None}
    await alarm_repo.record_history(
        session, alarm_id=alarm["id"], device_id=alarm["device_id"],
        action="jira_comment", severity=alarm.get("severity"),
        actor=decision.actor,
        detail={"issue_key": decision.issue_key, "note": decision.note})
    return {"action": "comment", "issue_key": decision.issue_key,
            "alarm": alarm["id"]}


async def _orphaned(session: AsyncSession, integration: dict[str, Any],
                    cfg: dict[str, Any], link: dict[str, Any],
                    decision: webhook.Decision) -> dict[str, Any]:
    """The issue was deleted out from under us.

    The link goes, so the next occurrence opens a new ticket rather than
    commenting into a void. It is recorded at WARNING and audited because a
    condition somebody was told about has quietly stopped being tracked, and
    nothing else in either system would ever mention it.
    """
    await repo.drop_link(session, link["fingerprint"])
    await _audit(session, integration, decision, link,
                 before={"issue_key": decision.issue_key},
                 after={"outcome": "issue deleted in Jira; link dropped"})
    log.warning("a linked jira issue was deleted", issue=decision.issue_key,
                fingerprint=link["fingerprint"], actor=decision.actor)
    return {"action": "orphaned", "issue_key": decision.issue_key}


_HANDLERS = {
    "acknowledge": _acknowledge,
    "declined": _declined,
    "moved": _moved,
    "comment": _comment,
    "orphaned": _orphaned,
}


# ----------------------------------------------------------------- pieces

async def _current_alarm(session: AsyncSession, link: dict[str, Any]
                         ) -> dict[str, Any] | None:
    """The alarm this ticket is about, NOW.

    Resolved through the condition's identity rather than through the alarm
    row the link was opened for, because a condition that cleared and raised
    again is a new row and the same fault. Falls back to `alarm_id` for links
    written before the identity columns existed.
    """
    if link.get("alarm_type") and link.get("device_id"):
        found = await alarm_repo.open_alarm_for(
            session, device_id=link["device_id"],
            alarm_type=link["alarm_type"], instance=link.get("instance") or "")
        if found is not None:
            return found
    if link.get("alarm_id"):
        return await alarm_repo.get_alarm(session, link["alarm_id"])
    return None


async def _audit(session: AsyncSession, integration: dict[str, Any],
                 decision: webhook.Decision, link: dict[str, Any], *,
                 before: Any = None, after: Any = None) -> None:
    """Every inbound action is attributed to the Jira account that caused it.

    "The alarm was acknowledged" with no actor is the audit row that makes an
    incident review impossible, and an action that arrived over a webhook is
    exactly the one nobody will be able to account for later.
    """
    await audit.record(
        session, actor=decision.actor,
        action=f"integration.inbound.{decision.action}",
        target_type="jira_link", target_id=link["fingerprint"],
        before=before,
        after={**(after or {}), "integration": integration["name"],
               "issue_key": decision.issue_key})
