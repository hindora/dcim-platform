"""Sync runs: reading the estate from the device plane, and what each one did."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

_SELECT = """
    SELECT id::text, started_at, finished_at, status, actor, source,
           report, error,
           round(EXTRACT(EPOCH FROM (COALESCE(finished_at, now()) - started_at)))
               AS seconds
      FROM sim_import_run
"""


class AlreadyRunningError(RuntimeError):
    """A sync is in flight. Refused rather than queued.

    Two imports writing the whole estate at once would interleave their
    decommission sweeps, and each would read the other's half-written devices as
    absent - so the second run would retire hardware the first had not finished
    importing.
    """


async def start(session: AsyncSession, *, actor: str,
                source: str | None) -> dict[str, Any]:
    """Open a run, or refuse because one is already open.

    The refusal comes from the partial unique index, not from a SELECT followed by
    an INSERT: that pair races, and the whole point is that two of these must
    never overlap.
    """
    try:
        row = (await session.execute(text("""
            INSERT INTO sim_import_run (actor, source, status)
            VALUES (:actor, :source, 'running')
            RETURNING id::text, started_at, finished_at, status, actor, source,
                      report, error, 0 AS seconds
        """), {"actor": actor, "source": source})).mappings().one()
    except IntegrityError as exc:
        raise AlreadyRunningError("a sync is already running") from exc
    return dict(row)


async def finish(session: AsyncSession, run_id: str, *,
                 report: str | None = None, error: str | None = None) -> None:
    """Close a run. Always called, including on failure - a row left `running`
    for ever would block every future sync, which is a worse outcome than a
    recorded failure."""
    await session.execute(text("""
        UPDATE sim_import_run
           SET finished_at = now(),
               status = CASE WHEN :error IS NULL THEN 'completed' ELSE 'failed' END,
               report = CAST(:report AS jsonb),
               error = :error
         WHERE id = CAST(:id AS uuid)
    """), {"id": run_id, "report": report, "error": error})


async def get(session: AsyncSession, run_id: str) -> dict[str, Any] | None:
    row = (await session.execute(
        text(_SELECT + " WHERE id = CAST(:id AS uuid)"),
        {"id": run_id})).mappings().first()
    return dict(row) if row else None


async def recent(session: AsyncSession, limit: int = 10) -> list[dict[str, Any]]:
    rows = (await session.execute(
        text(_SELECT + " ORDER BY started_at DESC LIMIT :limit"),
        {"limit": limit})).mappings().all()
    return [dict(r) for r in rows]


async def running(session: AsyncSession) -> dict[str, Any] | None:
    """The run in flight, if any. What the UI polls while a sync is going."""
    row = (await session.execute(
        text(_SELECT + " WHERE status = 'running' LIMIT 1"))).mappings().first()
    return dict(row) if row else None


async def release_stale(session: AsyncSession, *, older_than_s: int = 3600) -> int:
    """Fail any run that cannot still be going.

    The importer lives in the API process, so a restart mid-import leaves a row
    `running` with nothing behind it - and the unique index then refuses every
    future sync. Swept on read rather than by a ticker: the only thing that cares
    is the next caller, and it is about to look anyway.
    """
    res = await session.execute(text("""
        UPDATE sim_import_run
           SET status = 'failed', finished_at = now(),
               error = COALESCE(error,
                   'the API restarted while this sync was running')
         WHERE status = 'running'
           AND started_at < now() - make_interval(secs => :age)
    """), {"age": older_than_s})
    return res.rowcount or 0
