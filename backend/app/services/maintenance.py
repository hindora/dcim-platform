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
from app.repositories import alarms as alarm_repo
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
    return {"activated": len(due["active"]), "completed": len(due["completed"])}


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
