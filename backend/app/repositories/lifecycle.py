"""Lifecycle transitions: the history, and the rules about what may follow what."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# What may follow what. Small enough to state, and stating it is the point: an
# operator who is refused needs to be told what IS allowed, and a matrix in one
# place can answer that.
#
# `decommissioned -> in_stock` is not a mistake. A machine pulled from service
# and kept as a spare is the ordinary path, and forbidding it makes people
# create a duplicate record for hardware that already exists.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "planned": ("in_stock", "installed", "decommissioned"),
    "in_stock": ("installed", "planned", "retired"),
    "installed": ("in_service", "in_stock", "decommissioned"),
    "in_service": ("maintenance", "decommissioned"),
    "maintenance": ("in_service", "decommissioned"),
    "decommissioned": ("retired", "in_stock"),
    "retired": (),
}


class IllegalTransitionError(ValueError):
    """Refused, with the allowed set so the caller can say what to do instead."""

    def __init__(self, current: str, requested: str):
        self.current = current
        self.requested = requested
        self.allowed = TRANSITIONS.get(current, ())
        super().__init__(
            f"{current} cannot go to {requested}; "
            f"allowed: {', '.join(self.allowed) or 'none, this state is terminal'}")


async def history(session: AsyncSession, device_id: str,
                  limit: int = 100) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT id::text, from_state::text AS from_state, to_state::text AS to_state,
               reason, change_ref, actor, ts, attributes
        FROM device_lifecycle_event
        WHERE device_id = CAST(:id AS uuid)
        ORDER BY ts DESC, id
        LIMIT :limit
    """), {"id": device_id, "limit": limit})).mappings().all()
    return [dict(r) for r in rows]


async def current_state(session: AsyncSession, device_id: str) -> str | None:
    return (await session.execute(text(
        "SELECT lifecycle::text FROM device WHERE id = CAST(:id AS uuid)"
    ), {"id": device_id})).scalar_one_or_none()


async def record_transition(session: AsyncSession, *, device_id: str,
                            from_state: str, to_state: str, actor: str,
                            reason: str | None = None,
                            change_ref: str | None = None) -> dict[str, Any]:
    """Move the device and write the event, in the caller's transaction.

    `commissioned_at` and `decommissioned_at` are kept in step rather than
    retired: plenty of the platform still reads them, and they are now a
    denormalisation OF this table rather than the only record.
    """
    await session.execute(text("""
        UPDATE device
           SET lifecycle = CAST(:to_state AS lifecycle_t),
               commissioned_at = CASE WHEN :to_state = 'in_service'
                                        AND commissioned_at IS NULL
                                      THEN now() ELSE commissioned_at END,
               decommissioned_at = CASE WHEN :to_state = 'decommissioned' THEN now()
                                        WHEN :to_state IN ('in_service','installed',
                                                           'maintenance')
                                        THEN NULL
                                        ELSE decommissioned_at END,
               updated_at = now()
         WHERE id = CAST(:id AS uuid)
    """), {"id": device_id, "to_state": to_state})

    row = (await session.execute(text("""
        INSERT INTO device_lifecycle_event
            (device_id, from_state, to_state, reason, change_ref, actor)
        VALUES (CAST(:id AS uuid), CAST(:from_state AS lifecycle_t),
                CAST(:to_state AS lifecycle_t), :reason, :change_ref, :actor)
        RETURNING id::text, from_state::text AS from_state,
                  to_state::text AS to_state, reason, change_ref, actor, ts
    """), {"id": device_id, "from_state": from_state, "to_state": to_state,
           "reason": reason, "change_ref": change_ref,
           "actor": actor})).mappings().one()
    return dict(row)
