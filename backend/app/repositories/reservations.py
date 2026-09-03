"""Held capacity: rack units and power that nothing occupies yet.

The U range is enforced by `device_u_no_overlap`, not by anything here. A
reservation that names rack units inserts a `planned` device to stand in for
them, and PostgreSQL rejects the overlap - with no cross-table trigger, no
locking to get wrong, and the rack elevation rendering the hold for free.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

#: The device type a placeholder is created as. Real, so the elevation can draw
#: it; `planned`, so nothing polls it and no alarm is ever raised against it.
PLACEHOLDER_TYPE = "server"


class ReservationConflictError(ValueError):
    """Those rack units are already spoken for.

    Carries the occupier's name rather than the constraint's. An operator needs
    to know WHAT is in U20-24, not that a GiST exclusion constraint fired.
    """

    def __init__(self, message: str):
        super().__init__(message)


_SELECT = """
    SELECT r.id::text, r.project, r.owner_group, r.u_start, r.u_height,
           r.power_kw, r.cool_kw, r.needed_by, r.expires_at, r.status, r.notes,
           r.created_by, r.created_at,
           r.rack_id::text AS rack_id, rk.name AS rack_name,
           r.room_id::text AS room_id, rm.name AS room_name,
           dc.code AS datacenter_code,
           r.placeholder_device_id::text AS placeholder_device_id,
           (r.expires_at < CURRENT_DATE) AS overdue,
           (r.expires_at - CURRENT_DATE) AS days_left
    FROM capacity_reservation r
    LEFT JOIN rack rk ON rk.id = r.rack_id
    LEFT JOIN rack_row rr ON rr.id = rk.row_id
    LEFT JOIN room rm ON rm.id = COALESCE(rr.room_id, r.room_id)
    LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
"""


async def list_reservations(session: AsyncSession, *, status: str | None = None,
                            rack_id: str | None = None,
                            project: str | None = None,
                            limit: int = 200) -> list[dict[str, Any]]:
    where, params = [], {"limit": limit}
    if status:
        where.append("r.status = :status")
        params["status"] = status
    if rack_id:
        where.append("r.rack_id = CAST(:rack_id AS uuid)")
        params["rack_id"] = rack_id
    if project:
        where.append("r.project = :project")
        params["project"] = project

    sql = _SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    # Expired and expiring first. The failure mode of this feature is a hold
    # nobody released, so the ones rotting are the ones to show.
    sql += " ORDER BY (r.status = 'held') DESC, r.expires_at LIMIT :limit"
    rows = (await session.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


async def get(session: AsyncSession, reservation_id: str) -> dict[str, Any] | None:
    row = (await session.execute(
        text(_SELECT + " WHERE r.id = CAST(:id AS uuid)"),
        {"id": reservation_id})).mappings().first()
    return dict(row) if row else None


async def occupant_of(session: AsyncSession, rack_id: str, u_start: int,
                      u_height: int) -> str | None:
    """What already sits in that range, for the refusal message."""
    return (await session.execute(text("""
        SELECT d.name FROM device d
        WHERE d.rack_id = CAST(:rack AS uuid) AND d.u_start IS NOT NULL
          AND int4range(d.u_start, d.u_start + d.u_height, '[)')
              && int4range(CAST(:u_start AS integer),
                           CAST(:u_start AS integer) + CAST(:u_height AS integer),
                           '[)')
        ORDER BY d.u_start
        LIMIT 1
    """), {"rack": rack_id, "u_start": u_start,
           "u_height": u_height})).scalar_one_or_none()


async def create(session: AsyncSession, *, project: str, expires_at: Any,
                 created_by: str, rack_id: str | None = None,
                 room_id: str | None = None, u_start: int | None = None,
                 u_height: int | None = None, power_kw: float | None = None,
                 cool_kw: float | None = None, needed_by: Any = None,
                 owner_group: str | None = None,
                 notes: str | None = None) -> str:
    placeholder = None

    if u_start is not None and rack_id:
        # The whole enforcement story. If these units are taken,
        # device_u_no_overlap refuses the INSERT and nothing else has to check.
        try:
            placeholder = (await session.execute(text("""
                INSERT INTO device (name, device_type, rack_id, u_start, u_height,
                                    lifecycle, attributes)
                VALUES (:name, :dtype, CAST(:rack AS uuid), :u_start, :u_height,
                        'planned', CAST(:attrs AS jsonb))
                RETURNING id::text
            """), {"name": f"RESERVED-{project}-U{u_start}",
                   "dtype": PLACEHOLDER_TYPE, "rack": rack_id,
                   "u_start": u_start, "u_height": u_height,
                   "attrs": '{"reservation": true}'})).scalar_one()
        except IntegrityError as exc:
            await session.rollback()
            if "device_u_no_overlap" in str(exc):
                who = await occupant_of(session, rack_id, u_start, u_height)
                raise ReservationConflictError(
                    f"U{u_start}-U{u_start + u_height - 1} is occupied"
                    + (f" by {who}" if who else "")) from None
            raise

    return (await session.execute(text("""
        INSERT INTO capacity_reservation
            (rack_id, room_id, project, owner_group, u_start, u_height,
             power_kw, cool_kw, needed_by, expires_at, created_by, notes,
             placeholder_device_id)
        VALUES (CAST(:rack_id AS uuid), CAST(:room_id AS uuid), :project,
                :owner_group, :u_start, :u_height, :power_kw, :cool_kw,
                :needed_by, :expires_at, :created_by, :notes,
                CAST(:placeholder AS uuid))
        RETURNING id::text
    """), {"rack_id": rack_id, "room_id": room_id, "project": project,
           "owner_group": owner_group, "u_start": u_start, "u_height": u_height,
           "power_kw": power_kw, "cool_kw": cool_kw, "needed_by": needed_by,
           "expires_at": expires_at, "created_by": created_by, "notes": notes,
           "placeholder": placeholder})).scalar_one()


async def release(session: AsyncSession, reservation_id: str,
                  status: str = "released") -> None:
    """Give the space back, and take the placeholder with it.

    Leaving the placeholder behind is the bug this is written to avoid: a
    `planned` device holding rack units against a reservation that no longer
    exists, which nobody can explain and nobody thinks to delete.
    """
    row = await get(session, reservation_id)
    if row is None:
        raise LookupError("no such reservation")
    await session.execute(text(
        "UPDATE capacity_reservation SET status = :status WHERE id = CAST(:id AS uuid)"
    ), {"id": reservation_id, "status": status})
    if row["placeholder_device_id"]:
        await session.execute(text(
            "DELETE FROM device WHERE id = CAST(:id AS uuid) AND lifecycle = 'planned'"
        ), {"id": row["placeholder_device_id"]})
        await session.execute(text(
            "UPDATE capacity_reservation SET placeholder_device_id = NULL "
            "WHERE id = CAST(:id AS uuid)"), {"id": reservation_id})


async def fulfil(session: AsyncSession, reservation_id: str, *,
                 name: str, device_type: str, actor: str) -> str:
    """Promote the placeholder into the real device.

    An UPDATE rather than delete-and-create, so the machine that lands keeps the
    reservation's own row - and never briefly vacates the U range it was holding,
    which is when somebody else's install would slip into it.
    """
    row = await get(session, reservation_id)
    if row is None:
        raise LookupError("no such reservation")
    if not row["placeholder_device_id"]:
        raise ValueError("this reservation holds no rack units to fulfil")

    await session.execute(text("""
        UPDATE device
           SET name = :name, device_type = :dtype, lifecycle = 'planned',
               attributes = attributes - 'reservation', updated_at = now()
         WHERE id = CAST(:id AS uuid)
    """), {"id": row["placeholder_device_id"], "name": name, "dtype": device_type})
    await session.execute(text(
        "UPDATE capacity_reservation SET status = 'fulfilled' "
        "WHERE id = CAST(:id AS uuid)"), {"id": reservation_id})
    return row["placeholder_device_id"]


async def expire_due(session: AsyncSession) -> list[str]:
    """Mark held reservations whose date has passed, and free their units.

    Reservations that expire silently are how a rack stays held for a project
    cancelled two years ago.
    """
    rows = (await session.execute(text("""
        SELECT id::text FROM capacity_reservation
        WHERE status = 'held' AND expires_at < CURRENT_DATE
    """))).scalars().all()
    for reservation_id in rows:
        await release(session, reservation_id, status="expired")
    return list(rows)


async def held_summary(session: AsyncSession) -> dict[str, Any]:
    row = (await session.execute(text("""
        SELECT count(*) FILTER (WHERE status = 'held')                    AS held,
               count(*) FILTER (WHERE status = 'held'
                                  AND expires_at < CURRENT_DATE)          AS overdue,
               COALESCE(sum(u_height) FILTER (WHERE status = 'held'), 0)  AS u_held,
               COALESCE(sum(power_kw) FILTER (WHERE status = 'held'), 0)  AS kw_held
        FROM capacity_reservation
    """))).mappings().one()
    return dict(row)


_CONSTRAINT = re.compile(r'constraint "([a-z_]+)"')


def constraint_name(exc: Exception) -> str | None:
    match = _CONSTRAINT.search(str(exc))
    return match.group(1) if match else None
