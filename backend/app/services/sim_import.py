"""Run the topology importer from the API, in the background, once at a time.

The importer was a shell command. Making it a button needs three things the CLI
did not: it has to run without holding the request open for ninety seconds, it
has to be attributable, and a second one must not be able to start on top of the
first.

WHY ITS OWN SESSION. The task outlives the request that started it, and the
request's session is closed when the response is sent. Borrowing it would fail
somewhere in the middle of the estate, having already written part of it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import unit_of_work
from app.importer.simulator import TopologyImporter, fetch_topology
from app.repositories import sim_import as repo
from app.repositories.sim_import import AlreadyRunningError

log = get_logger("sim-import")

__all__ = ["AlreadyRunningError", "NotConfiguredError", "start", "status"]

#: How long a `running` row may sit before it is assumed dead. The import takes
#: ~90s against a full estate; an hour is generous enough that a slow run is
#: never killed and short enough that an API restart does not block syncing for a
#: working day.
STALE_AFTER_S = 3600


class NotConfiguredError(RuntimeError):
    """The simulator's address or credentials are unset.

    Raised with the setting NAME in the message, because "connection refused" for
    a URL the operator never saw is the least useful error this can produce - and
    `host.docker.internal` resolving nowhere outside a container is exactly the
    kind of default that survives in a .env for months.
    """


def _config() -> tuple[str, str, str]:
    s = get_settings()
    base = (s.simulator_base_url or "").strip()
    user = (s.simulator_username or "").strip()
    pwd = s.simulator_password.get_secret_value() if s.simulator_password else ""
    missing = [name for name, val in (("DCIM_SIMULATOR_BASE_URL", base),
                                      ("DCIM_SIMULATOR_USERNAME", user),
                                      ("DCIM_SIMULATOR_PASSWORD", pwd))
               if not val]
    if missing:
        raise NotConfiguredError(
            f"{', '.join(missing)} is not set in deploy/.env, so the DCIM does "
            f"not know where the device plane is or how to log in to it")
    return base, user, pwd


async def status(session: AsyncSession) -> dict[str, Any]:
    """What the Sync button needs to render: what is going, and what last ran."""
    await repo.release_stale(session, older_than_s=STALE_AFTER_S)
    await session.commit()
    configured, why = True, None
    try:
        _config()
    except NotConfiguredError as exc:
        configured, why = False, str(exc)
    return {
        "configured": configured,
        "not_configured_reason": why,
        "running": await repo.running(session),
        "recent": await repo.recent(session),
    }


async def start(session: AsyncSession, *, actor: str) -> dict[str, Any]:
    """Open a run and hand the work to a background task.

    The row is committed BEFORE the task starts, so the refusal of a second sync
    is already durable when this returns - and so a caller polling immediately
    sees the run it just created rather than nothing.
    """
    await repo.release_stale(session, older_than_s=STALE_AFTER_S)
    base, user, pwd = _config()
    run = await repo.start(session, actor=actor, source=base)
    await session.commit()
    # Fire and forget, with the reference kept so the task is not garbage
    # collected mid-flight - asyncio only holds a weak reference to it.
    task = asyncio.create_task(_run(run["id"], base, user, pwd))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return run


#: Strong references to in-flight tasks. Without this the event loop may collect
#: a task that nothing else holds, and the import stops half way with no error.
_TASKS: set = set()


async def _run(run_id: str, base: str, user: str, pwd: str) -> None:
    """The background half. Never raises: a failure has to reach the row, or the
    run stays `running` for ever and blocks every later sync."""
    report: dict[str, Any] | None = None
    error: str | None = None
    try:
        topology = await fetch_topology(base, user, pwd)
        async with unit_of_work() as s:
            importer = TopologyImporter(s)
            report = (await importer.run(topology)).as_dict()
        log.info("sync completed", run_id=run_id,
                 devices=(report or {}).get("devices"))
    # Broad on purpose: see the docstring. A failure that escaped here would
    # leave the row `running` and block every future sync.
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:2000]
        log.warning("sync failed", run_id=run_id, error=error)
    finally:
        try:
            async with unit_of_work() as s:
                await repo.finish(
                    s, run_id,
                    report=json.dumps(report, default=str) if report else None,
                    error=error)
        except Exception as exc:
            # Nothing left to do but say so. The stale sweep will release the row.
            log.error("could not close sync run", run_id=run_id, error=str(exc))
