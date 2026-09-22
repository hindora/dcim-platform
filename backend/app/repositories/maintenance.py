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
           w.jira_issue_key, w.jira_integration_id::text AS jira_integration_id,
           w.jira_approval_state, w.require_approval,
           -- Why this window has not opened although its time has come. A
           -- window that silently fails to start reads as a DCIM fault, and
           -- the engineer is already at the door.
           CASE WHEN w.require_approval AND w.status = 'scheduled'
                 AND w.starts_at <= now()
                 AND w.jira_approval_state IS DISTINCT FROM 'approved'
                THEN coalesce(w.jira_issue_key, 'its change request')
                     || ' has not been approved'
           END AS blocked_reason,
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
                        suppress: bool = True,
                        require_approval: bool = False) -> str:
    return (await session.execute(text("""
        INSERT INTO maintenance_window
            (title, description, change_ref, kind, starts_at, ends_at,
             suppress, created_by, require_approval,
             jira_approval_state)
        VALUES (:title, :description, :change_ref, :kind, :starts_at, :ends_at,
                :suppress, :created_by, :require_approval,
                -- `pending` only when somebody is actually being waited on.
                -- NULL on a window that never asked is not the same as "not
                -- yet approved", and a console that showed both the same way
                -- would have every window looking like it was stuck.
                CASE WHEN :require_approval THEN 'pending' END)
        RETURNING id::text
    """), {"title": title, "description": description, "change_ref": change_ref,
           "kind": kind, "starts_at": starts_at, "ends_at": ends_at,
           "suppress": suppress, "created_by": created_by,
           "require_approval": require_approval})).scalar_one()


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
               COALESCE(ds.max_severity::text, 'CLEAR') AS max_severity,
               -- Where each machine stands. A change request that lists forty
               -- device names and no locations is a list the engineer has to
               -- take back to the DCIM before they can walk anywhere.
               dc.code AS datacenter_code, rm.name AS room_name,
               r.name AS rack_name
        FROM maintenance_target t
        JOIN device d ON d.id = t.device_id
        LEFT JOIN device_state ds ON ds.device_id = d.id
        LEFT JOIN rack r        ON r.id = d.rack_id
        LEFT JOIN rack_row rr   ON rr.id = r.row_id
        LEFT JOIN room rm       ON rm.id = COALESCE(rr.room_id, d.room_id)
        LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
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
        WHERE (status = 'scheduled' AND starts_at <= now()
               -- THE GATE. Expressed here rather than as a check in the
               -- service, for the same reason `status` is a column and not a
               -- comparison against now(): one process decides, and a
               -- predicate in SQL cannot be forgotten by the next person who
               -- adds a transition.
               AND (NOT require_approval OR jira_approval_state = 'approved'))
           -- A window that was never allowed to open still has to close, or a
           -- declined change leaves a scheduled row sitting in the table for
           -- ever waiting for a start that will never come.
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


# ------------------------------------------------------- change requests

async def set_change(session: AsyncSession, window_id: str, *,
                     issue_key: str | None, integration_id: str | None,
                     approval_state: str | None) -> None:
    """Record the change request this platform opened for a window."""
    await session.execute(text("""
        UPDATE maintenance_window
           SET jira_issue_key = :issue_key,
               jira_integration_id = CAST(:integration_id AS uuid),
               jira_approval_state = :approval_state,
               updated_at = now()
         WHERE id = CAST(:id AS uuid)
    """), {"id": window_id, "issue_key": issue_key,
           "integration_id": integration_id, "approval_state": approval_state})


async def set_approval(session: AsyncSession, window_id: str,
                       state: str) -> dict[str, Any] | None:
    """Move a window's approval, and say what it was before.

    Returns None when nothing moved, which is the common case: a change
    request passes through half a dozen statuses and only two of them mean
    anything here, so most inbound events are correctly a no-op.
    """
    row = (await session.execute(text("""
        UPDATE maintenance_window
           SET jira_approval_state = :state, updated_at = now()
         WHERE id = CAST(:id AS uuid)
           AND jira_approval_state IS DISTINCT FROM :state
        RETURNING id::text, title, status
    """), {"id": window_id, "state": state})).mappings().first()
    return dict(row) if row else None


async def window_by_issue(session: AsyncSession, integration_id: str,
                          issue_key: str) -> dict[str, Any] | None:
    """The window a change request belongs to.

    Asked by the inbound path for every event whose issue key matched no
    condition. One partial-index probe, on the critical path of a request that
    must answer inside 30 seconds.
    """
    row = (await session.execute(text("""
        SELECT id::text, title, status, kind, starts_at, ends_at,
               require_approval, jira_approval_state, jira_issue_key,
               suppress, created_by
          FROM maintenance_window
         WHERE jira_issue_key = :key
           AND jira_integration_id = CAST(:integration_id AS uuid)
    """), {"key": issue_key, "integration_id": integration_id})
           ).mappings().first()
    return dict(row) if row else None


async def awaiting_approval(session: AsyncSession) -> list[dict[str, Any]]:
    """Windows the clock has reached that approval is still holding shut.

    The ticker skips these, and something has to say so: the engineer is
    already at the door, and a window that silently fails to open reads as a
    DCIM fault rather than as an outstanding approval.
    """
    rows = (await session.execute(text("""
        SELECT id::text, title, jira_issue_key, jira_approval_state, starts_at,
               EXTRACT(EPOCH FROM (now() - starts_at)) AS overdue_s
          FROM maintenance_window
         WHERE require_approval AND status = 'scheduled' AND starts_at <= now()
           AND jira_approval_state IS DISTINCT FROM 'approved'
         ORDER BY starts_at
    """))).mappings().all()
    return [dict(r) for r in rows]


async def completion_report(session: AsyncSession, window_id: str
                            ) -> dict[str, Any]:
    """What the window cost, for the comment posted when it closes.

    Counted BEFORE `unshelve` runs, because un-shelving is what makes the
    still-open ones visible again and the numbers would change underneath the
    report otherwise.
    """
    row = (await session.execute(text("""
        SELECT count(*) AS shelved,
               count(*) FILTER (WHERE a.state = 'CLEARED') AS cleared
          FROM alarm a
         WHERE a.shelved_by_window = CAST(:id AS uuid)
    """), {"id": window_id})).mappings().one()

    still = (await session.execute(text("""
        SELECT a.id::text, a.alarm_type, a.severity::text AS severity,
               a.message, d.name AS device_name
          FROM alarm a
          LEFT JOIN device d ON d.id = a.device_id
         WHERE a.shelved_by_window = CAST(:id AS uuid) AND a.state <> 'CLEARED'
         ORDER BY a.severity DESC, a.first_seen
         LIMIT 50
    """), {"id": window_id})).mappings().all()

    return {"shelved": int(row["shelved"]), "cleared": int(row["cleared"]),
            "still_open": [dict(r) for r in still]}
