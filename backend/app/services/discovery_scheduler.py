"""The loop that queues scheduled sweeps.

It lives in the API process because queueing a sweep is a row insert and nothing
more: the collector, which is on the management network, still claims and runs it
like any other. The API never touches a device.

Safe with several API processes: the due schedule is claimed FOR UPDATE SKIP
LOCKED, so the second process moves on instead of queueing a duplicate.
"""

from __future__ import annotations

import asyncio

from app.core.logging import get_logger
from app.db.session import unit_of_work
from app.services import discovery as service

log = get_logger("discovery.scheduler")

#: How often to look. A schedule is hours apart, so a minute of lateness is noise.
TICK_S = 60

#: The first look waits: the API should be serving before it starts writing, and a
#: test that opens the app for one request must not find a scheduler in its DB.
FIRST_TICK_S = 30


async def run_forever(stop: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=FIRST_TICK_S)
        return
    except TimeoutError:
        pass
    while not stop.is_set():
        try:
            async with unit_of_work() as session:
                fired = await service.fire_due_schedule(session)
            if fired:
                log.info("schedule fired", run_id=fired["run"]["id"])
        except Exception as exc:  # the loop must outlive one bad tick
            log.warning("scheduler tick failed", error=str(exc))
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_S)
        except TimeoutError:
            pass
