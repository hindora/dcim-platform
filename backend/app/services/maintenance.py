"""Maintenance windows: status transitions, shelving, and the preview.

The ordering in `activate` and `complete` is the part that matters. Shelving
without recomputing `device_state` leaves every rack and room roll-up reading a
severity that came from an alarm nobody can see any more, which is worse than
not shelving at all - the console would show a red room and an empty alarm list.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.integrations import change
from app.repositories import alarms as alarm_repo
from app.repositories import integrations as integrations_repo
from app.repositories import maintenance as repo

log = get_logger("maintenance")


class MaintenanceError(ValueError):
    """Bad request, with a message meant for the caller."""


async def activate(session: AsyncSession, window_id: str) -> int:
    """Start a window: mark what is already standing, then fix the roll-ups."""
    targets = await repo.targets(session, window_id)
    await repo.set_status(session, window_id, "active")
    shelved = await repo.shelve_open_alarms(session, window_id)
    if targets:
        await alarm_repo.refresh_device_alarm_state(
            session, [t["id"] for t in targets])
    log.info("maintenance window active", window_id=window_id,
             targets=len(targets), shelved=shelved)
    return shelved


async def complete(session: AsyncSession, window_id: str,
                   status: str = "completed") -> list[str]:
    """End a window and put back what is still wrong.

    Only alarms still OPEN are un-shelved. One that cleared during the work
    stays marked: un-marking it would push it into the active list as
    freshly-visible history, and an operator reading the console after a window
    wants what is wrong now.
    """
    # Before `unshelve`, which is what makes the still-open alarms visible
    # again: gathering the report afterwards would count a different set.
    await report_completion(session, window_id, status)
    await repo.set_status(session, window_id, status)
    devices = await repo.unshelve(session, window_id)
    if devices:
        await alarm_repo.refresh_device_alarm_state(session, devices)
    log.info("maintenance window ended", window_id=window_id, status=status,
             unshelved_devices=len(devices))
    return devices


async def run_due_transitions(session: AsyncSession) -> dict[str, int]:
    """The ticker step. Advances every window the clock has caught up with.

    Status is a column rather than a comparison against now() precisely so this
    exists: one process decides, and the ingest worker and the API then read the
    same answer instead of each evaluating their own clock.
    """
    due = await repo.due_transitions(session)
    for window_id in due["active"]:
        await activate(session, window_id)
    for window_id in due["completed"]:
        await complete(session, window_id)

    # What the gate is holding shut. The ticker's own query skips these, and
    # something has to say so out loud: the engineer is already at the door,
    # and a window that silently fails to open reads as a DCIM fault rather
    # than as an approval nobody has given.
    held = await repo.awaiting_approval(session)
    for window in held:
        log.warning("window held: its change request is not approved",
                    window_id=window["id"], title=window["title"],
                    issue=window["jira_issue_key"],
                    approval=window["jira_approval_state"],
                    overdue_min=int((window["overdue_s"] or 0) // 60))

    return {"activated": len(due["active"]), "completed": len(due["completed"]),
            "held": len(held)}


async def preview(session: AsyncSession, device_ids: list[str]) -> dict[str, Any]:
    """What this window would actually cover, before anybody commits to it.

    A window scoped too widely is otherwise discovered at 02:00. Everything here
    comes from traversals that already exist - the impact graph and the power
    chain - so this adds a screen, not a second implementation of reachability.
    """
    from app.services import power as power_service
    from app.services import topology as topology_service

    if not device_ids:
        return {"devices": 0, "downstream_devices": 0, "cut_off": 0,
                "alarms_currently_active": 0, "redundancy_warnings": []}

    selected = set(device_ids)
    downstream: set[str] = set()
    cut_off: set[str] = set()
    warnings: list[dict[str, str]] = []

    for device_id in device_ids:
        try:
            impact = await topology_service.get_impact(session, device_id)
        except Exception:
            impact = None
        for layer in getattr(impact, "layers", []) or []:
            # cut_off and degraded are counted apart because they are different
            # events: one goes dark, the other survives on fewer feeds. A single
            # "affected" number would let a window that darkens twelve machines
            # read the same as one that costs them a redundant side.
            for node in layer.cut_off:
                if node.device_id not in selected:
                    downstream.add(node.device_id)
                    cut_off.add(node.device_id)
            for node in layer.degraded:
                if node.device_id not in selected:
                    downstream.add(node.device_id)

        try:
            chain = await power_service.chain_for(session, device_id)
        except Exception:
            continue
        # SINGLE_FEED and NO_FEED, spelled as the power service spells them. A
        # load already on one side loses power when its feeder enters the
        # window, and that is the sentence worth reading before committing.
        if chain and chain.get("redundancy") in (
                power_service.SINGLE_FEED, power_service.NO_FEED):
            warnings.append({
                "device_id": device_id,
                "redundancy": chain["redundancy"],
                "reason": chain.get("reason") or "not redundantly fed",
            })

    active = await alarm_repo.list_alarms(
        session, states=["ACTIVE", "ACKNOWLEDGED"], limit=500)
    on_targets = [a for a in active if a.get("device_id") in selected]

    return {
        "devices": len(device_ids),
        "downstream_devices": len(downstream),
        "cut_off": len(cut_off),
        "alarms_currently_active": len(on_targets),
        "redundancy_warnings": warnings,
    }


# ----------------------------------------------------------- change requests
#
# The half no surveyed DCIM product ships. What makes it worth building is not
# that a window can open a ticket - anything can open a ticket - but that the
# DCIM is the only system that can tell a change advisory board what the window
# actually costs: how many alarms it silences, how many machines it darkens,
# and which redundant side it removes. `preview()` above already computes all
# three.


async def request_change(session: AsyncSession, window_id: str, *,
                         integration_id: str | None = None) -> dict[str, Any]:
    """Queue a change request for a window, with its impact attached.

    Queued rather than created: the HTTP call belongs to the dispatcher, so
    the honest answer here is "it is on its way", and a window is not left
    half-created because somebody's service desk was slow.
    """
    window = await repo.get_window(session, window_id)
    if window is None:
        raise MaintenanceError("no such window")
    if window.get("jira_issue_key"):
        raise MaintenanceError(
            f"this window already has {window['jira_issue_key']}")
    if window["status"] not in ("scheduled",):
        raise MaintenanceError(
            "a change request is asked for before the work, not after it")

    integration = await _integration_for(session, integration_id)
    if integration is None:
        raise MaintenanceError("no ticketing integration is enabled")

    targets = await repo.targets(session, window_id)
    impact = await preview(session, [t["id"] for t in targets])

    await integrations_repo.enqueue_window(
        session, integration_id=integration["id"], window_id=window_id,
        kind="window_requested",
        payload={"window": window, "targets": targets, "preview": impact})
    log.info("change request queued", window_id=window_id,
             integration=integration["name"], targets=len(targets))
    return {"queued": True, "integration": integration["name"],
            "targets": len(targets), "preview": impact}


async def report_completion(session: AsyncSession, window_id: str,
                            outcome: str) -> None:
    """Post what the window cost, if it has a change request to post it on.

    The report is gathered BEFORE `complete` un-shelves anything, because
    un-shelving is what makes the still-open alarms visible again and the
    numbers would move underneath the comment otherwise.
    """
    window = await repo.get_window(session, window_id)
    if window is None or not window.get("jira_issue_key"):
        return
    integration_id = window.get("jira_integration_id")
    if not integration_id:
        return
    report = await repo.completion_report(session, window_id)
    report["outcome"] = outcome
    await integrations_repo.enqueue_window(
        session, integration_id=integration_id, window_id=window_id,
        kind="window_completed" if outcome == "completed" else "window_cancelled",
        payload={"window": window, "report": report})


async def apply_approval(session: AsyncSession, window_id: str,
                         state: str) -> dict[str, Any] | None:
    """Record a decision from the change request, and act on a refusal.

    A DECLINE cancels the window. It has to: the work is not happening, and a
    window left scheduled would open at 02:00 and shelve alarms on equipment
    nobody is touching - which is the failure this whole gate exists to
    prevent, arrived at from the other direction.

    An approval only unlocks the gate. It does NOT start the window early; the
    ticker still waits for `starts_at`, because "approved" and "due" are
    different facts and a board approving at noon for a 02:00 window has not
    asked for it to begin now.
    """
    moved = await repo.set_approval(session, window_id, state)
    if moved is None:
        return None
    if state == change.DECLINED and moved["status"] == "scheduled":
        await repo.set_status(session, window_id, "cancelled")
        log.warning("window cancelled: its change request was declined",
                    window_id=window_id, title=moved["title"])
        moved["cancelled"] = True
    else:
        log.info("window approval recorded", window_id=window_id, state=state)
    return moved


async def _integration_for(session: AsyncSession, integration_id: str | None
                           ) -> dict[str, Any] | None:
    if integration_id:
        row = await integrations_repo.get_integration(session, integration_id)
        return row if row and row["enabled"] else None
    return await integrations_repo.enabled_integration(session)
