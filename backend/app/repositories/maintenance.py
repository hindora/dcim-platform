"""Maintenance windows, their targets, and the shelving they perform.

The shelving is the part worth reading. A window does not stop alarms being
raised - the engine runs untouched - it marks the ones raised on its targets
while it is running, and every query an operator reads as "what is wrong now"
excludes marked rows. See migration 0046 for why that is shelving rather than
suppression.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_WINDOW_SELECT = """
    SELECT w.id::text, w.title, w.description, w.change_ref, w.kind,
           w.starts_at, w.ends_at, w.status, w.suppress,
           w.created_by, w.created_at, w.updated_at,
           (SELECT count(*) FROM maintenance_target t WHERE t.window_id = w.id)
               AS target_count,
           (SELECT count(*) FROM alarm a WHERE a.shelved_by_window = w.id)
               AS shelved_alarms
    FROM maintenance_window w
"""


async def list_windows(session: AsyncSession, *, status: str | None = None,
                       device_id: str | None = None,
                       limit: int = 100) -> list[dict[str, Any]]:
    where, params = [], {"limit": limit}
    if status:
        where.append("w.status = :status")
        params["status"] = status
    if device_id:
        where.append("EXISTS (SELECT 1 FROM maintenance_target t "
                     "WHERE t.window_id = w.id "
                     "AND t.device_id = CAST(:device_id AS uuid))")
        params["device_id"] = device_id
    sql = _WINDOW_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY w.starts_at DESC LIMIT :limit"
    rows = (await session.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


async def get_window(session: AsyncSession, window_id: str) -> dict[str, Any] | None:
    row = (await session.execute(
        text(_WINDOW_SELECT + " WHERE w.id = CAST(:id AS uuid)"),
        {"id": window_id})).mappings().first()
    return dict(row) if row else None


async def create_window(session: AsyncSession, *, title: str, starts_at: Any,
                        ends_at: Any, created_by: str,
                        description: str | None = None,
                        change_ref: str | None = None,
                        kind: str = "planned",
                        suppress: bool = True) -> str:
    return (await session.execute(text("""
        INSERT INTO maintenance_window
            (title, description, change_ref, kind, starts_at, ends_at,
             suppress, created_by)
        VALUES (:title, :description, :change_ref, :kind, :starts_at, :ends_at,
                :suppress, :created_by)
        RETURNING id::text
    """), {"title": title, "description": description, "change_ref": change_ref,
           "kind": kind, "starts_at": starts_at, "ends_at": ends_at,
           "suppress": suppress, "created_by": created_by})).scalar_one()


async def set_targets(session: AsyncSession, window_id: str,
                      device_ids: list[str]) -> int:
    if not device_ids:
        return 0
    await session.execute(text("""
        INSERT INTO maintenance_target (window_id, device_id)
        SELECT CAST(:wid AS uuid), CAST(d AS uuid)
        FROM unnest(CAST(:ids AS text[])) AS d
        ON CONFLICT DO NOTHING
    """), {"wid": window_id, "ids": device_ids})
    return len(device_ids)


async def remove_target(session: AsyncSession, window_id: str,
                        device_id: str) -> None:
    await session.execute(text("""
        DELETE FROM maintenance_target
        WHERE window_id = CAST(:wid AS uuid) AND device_id = CAST(:did AS uuid)
    """), {"wid": window_id, "did": device_id})


async def targets(session: AsyncSession, window_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT d.id::text, d.name, d.device_type,
               COALESCE(ds.max_severity::text, 'CLEAR') AS max_severity
        FROM maintenance_target t
        JOIN device d ON d.id = t.device_id
        LEFT JOIN device_state ds ON ds.device_id = d.id
        WHERE t.window_id = CAST(:wid AS uuid)
        ORDER BY d.name
    """), {"wid": window_id})).mappings().all()
    return [dict(r) for r in rows]


async def set_status(session: AsyncSession, window_id: str, status: str) -> None:
    await session.execute(text("""
        UPDATE maintenance_window SET status = :status, updated_at = now()
        WHERE id = CAST(:id AS uuid)
    """), {"id": window_id, "status": status})


# ------------------------------------------------------------------ shelving

async def shelve_open_alarms(session: AsyncSession, window_id: str) -> int:
    """Mark alarms already standing on this window's targets.

    Run when a window becomes active. Alarms raised DURING the window are marked
    at raise time by the engine; this catches the ones that were already there,
    which is the common case - work is scheduled because something is wrong.
    """
    return (await session.execute(text("""
        UPDATE alarm a
           SET shelved_by_window = CAST(:wid AS uuid)
         WHERE a.state <> 'CLEARED'
           AND a.shelved_by_window IS NULL
           AND a.device_id IN (SELECT device_id FROM maintenance_target
                               WHERE window_id = CAST(:wid AS uuid))
    """), {"wid": window_id})).rowcount or 0


async def unshelve(session: AsyncSession, window_id: str) -> list[str]:
    """Release the mark, and say which devices need their roll-up recomputed.

    Alarms that CLEARED during the window stay marked. Un-marking them would
    resurrect them into the active list as freshly-visible history, and an
    operator reading the console after a window wants what is wrong now, not a
    replay of what broke and recovered while the engineers were in there. The
    window's own page still lists them.
    """
    rows = (await session.execute(text("""
        UPDATE alarm a
           SET shelved_by_window = NULL
         WHERE a.shelved_by_window = CAST(:wid AS uuid)
           AND a.state <> 'CLEARED'
        RETURNING a.device_id::text
    """), {"wid": window_id})).scalars().all()
    return list(set(rows))


async def shelved_alarms(session: AsyncSession, window_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT a.id::text, a.alarm_type, a.severity::text AS severity,
               a.state::text AS state, a.message, a.first_seen,
               d.name AS device_name, d.id::text AS device_id
        FROM alarm a JOIN device d ON d.id = a.device_id
        WHERE a.shelved_by_window = CAST(:wid AS uuid)
        ORDER BY a.severity DESC, a.first_seen
    """), {"wid": window_id})).mappings().all()
    return [dict(r) for r in rows]


async def active_window_for(session: AsyncSession,
                            device_ids: list[str]) -> dict[str, str]:
    """device_id -> window_id for devices currently inside a suppressing window.

    The engine's lookup at raise time. Reads `status`, not a comparison against
    now(): the worker and the API must agree about whether a window is running,
    and two processes reading their own clocks do not.
    """
    if not device_ids:
        return {}
    rows = (await session.execute(text("""
        SELECT t.device_id::text AS device_id, w.id::text AS window_id
        FROM maintenance_target t
        JOIN maintenance_window w ON w.id = t.window_id
        WHERE w.status = 'active' AND w.suppress
          AND t.device_id = ANY(CAST(:ids AS uuid[]))
    """), {"ids": device_ids})).mappings().all()
    return {r["device_id"]: r["window_id"] for r in rows}


async def due_transitions(session: AsyncSession) -> dict[str, list[str]]:
    """Windows whose status the clock says should change.

    The ticker's query. Returning ids rather than doing the update here keeps
    the side effects - shelving, un-shelving, recomputing roll-ups - in the
    service where they can be ordered.
    """
    rows = (await session.execute(text("""
        SELECT id::text, status,
               CASE WHEN status = 'scheduled' AND starts_at <= now() THEN 'active'
                    WHEN status = 'active' AND ends_at <= now() THEN 'completed'
               END AS next_status
        FROM maintenance_window
        WHERE (status = 'scheduled' AND starts_at <= now())
           OR (status = 'active' AND ends_at <= now())
    """))).mappings().all()
    out: dict[str, list[str]] = {"active": [], "completed": []}
    for r in rows:
        if r["next_status"]:
            out[r["next_status"]].append(r["id"])
    return out


# ------------------------------------------------------------------- records

async def list_records(session: AsyncSession, device_id: str,
                       limit: int = 100) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT r.id::text, r.window_id::text AS window_id, r.performed_at,
               r.performed_by, r.kind, r.summary, r.detail, r.parts_used,
               w.title AS window_title
        FROM maintenance_record r
        LEFT JOIN maintenance_window w ON w.id = r.window_id
        WHERE r.device_id = CAST(:id AS uuid)
        ORDER BY r.performed_at DESC
        LIMIT :limit
    """), {"id": device_id, "limit": limit})).mappings().all()
    return [dict(r) for r in rows]


async def add_record(session: AsyncSession, *, device_id: str, performed_by: str,
                     kind: str, summary: str, detail: str | None = None,
                     window_id: str | None = None,
                     parts_used: str = "[]") -> str:
    return (await session.execute(text("""
        INSERT INTO maintenance_record
            (device_id, window_id, performed_by, kind, summary, detail, parts_used)
        VALUES (CAST(:device_id AS uuid), CAST(:window_id AS uuid), :performed_by,
                :kind, :summary, :detail, CAST(:parts_used AS jsonb))
        RETURNING id::text
    """), {"device_id": device_id, "window_id": window_id,
           "performed_by": performed_by, "kind": kind, "summary": summary,
           "detail": detail, "parts_used": parts_used})).scalar_one()
