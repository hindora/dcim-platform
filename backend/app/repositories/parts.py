"""Consumable parts, stores, and the ledger that is the only way stock moves.

There is deliberately no `set_on_hand`. Every change is a movement and `on_hand`
is the running total, because a stock figure somebody can overwrite cannot
answer "we had four last week, where did they go" - and every discrepancy then
becomes one person's memory against a number.

Correcting a miscount is posting an `adjustment` with a note, which the schema
requires. That is the same operation wearing an honest name.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class InsufficientStockError(ValueError):
    """Consuming more than exists, refused with the numbers in the message."""

    def __init__(self, sku: str, wanted: int, have: int):
        self.sku, self.wanted, self.have = sku, wanted, have
        super().__init__(
            f"{sku}: asked for {wanted}, only {have} on hand at that store")


# ------------------------------------------------------------------- parts

_PART_SELECT = """
    SELECT p.id::text, p.sku, p.name, p.category, p.fits_types,
           p.unit_cost, p.currency,
           v.name AS vendor_name, p.vendor_id::text AS vendor_id,
           COALESCE(s.total, 0)     AS on_hand,
           COALESCE(s.reserved, 0)  AS reserved,
           s.reorder_at,
           -- Derived, not stored: a flag would need maintaining on every
           -- movement and would be wrong the moment one was missed.
           (s.reorder_at IS NOT NULL AND COALESCE(s.total, 0) <= s.reorder_at)
               AS below_reorder
    FROM part p
    LEFT JOIN vendor v ON v.id = p.vendor_id
    LEFT JOIN (
        SELECT part_id, sum(on_hand) AS total, sum(reserved) AS reserved,
               min(reorder_at) AS reorder_at
        FROM part_stock GROUP BY part_id
    ) s ON s.part_id = p.id
"""


async def list_parts(session: AsyncSession, *, category: str | None = None,
                     below_reorder: bool = False, search: str | None = None,
                     limit: int = 200) -> list[dict[str, Any]]:
    where, params = [], {"limit": limit}
    if category:
        where.append("p.category = :category")
        params["category"] = category
    if below_reorder:
        where.append("s.reorder_at IS NOT NULL AND COALESCE(s.total, 0) <= s.reorder_at")
    if search:
        where.append("(p.sku ILIKE :q OR p.name ILIKE :q)")
        params["q"] = f"%{search}%"

    sql = _PART_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    # Short stock first: the reason somebody opens this page.
    sql += (" ORDER BY (s.reorder_at IS NOT NULL "
            "AND COALESCE(s.total, 0) <= s.reorder_at) DESC, p.category, p.name"
            " LIMIT :limit")
    rows = (await session.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


async def get_part(session: AsyncSession, part_id: str) -> dict[str, Any] | None:
    row = (await session.execute(
        text(_PART_SELECT + " WHERE p.id = CAST(:id AS uuid)"),
        {"id": part_id})).mappings().first()
    return dict(row) if row else None


async def create_part(session: AsyncSession, **fields: Any) -> str:
    return (await session.execute(text("""
        INSERT INTO part (sku, name, category, vendor_id, fits_types,
                          unit_cost, currency)
        VALUES (:sku, :name, :category, CAST(:vendor_id AS uuid),
                CAST(:fits_types AS text[]), :unit_cost, :currency)
        RETURNING id::text
    """), fields)).scalar_one()


async def delete_part(session: AsyncSession, part_id: str) -> None:
    await session.execute(text("DELETE FROM part WHERE id = CAST(:id AS uuid)"),
                          {"id": part_id})


async def has_history(session: AsyncSession, part_id: str) -> bool:
    """Whether anything has ever moved. A part with a ledger is not deletable -
    removing it would take the history of what was fitted where with it."""
    return bool((await session.execute(text(
        "SELECT count(*) FROM stock_movement WHERE part_id = CAST(:id AS uuid)"
    ), {"id": part_id})).scalar_one())


# ------------------------------------------------------------------ stores

async def list_stores(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT s.id::text, s.name, s.location_note,
               s.datacenter_id::text AS datacenter_id, dc.code AS datacenter_code,
               s.room_id::text AS room_id, rm.name AS room_name,
               (SELECT count(*) FROM part_stock ps
                 WHERE ps.store_id = s.id AND ps.on_hand > 0) AS lines,
               (SELECT COALESCE(sum(on_hand), 0) FROM part_stock ps
                 WHERE ps.store_id = s.id) AS units
        FROM store s
        LEFT JOIN datacenter dc ON dc.id = s.datacenter_id
        LEFT JOIN room rm ON rm.id = s.room_id
        ORDER BY dc.code NULLS LAST, s.name
    """))).mappings().all()
    return [dict(r) for r in rows]


async def create_store(session: AsyncSession, **fields: Any) -> str:
    return (await session.execute(text("""
        INSERT INTO store (name, datacenter_id, room_id, location_note)
        VALUES (:name, CAST(:datacenter_id AS uuid), CAST(:room_id AS uuid),
                :location_note)
        RETURNING id::text
    """), fields)).scalar_one()


async def stock_of(session: AsyncSession, part_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT ps.store_id::text, st.name AS store_name, dc.code AS datacenter_code,
               ps.on_hand, ps.reserved, ps.reorder_at, ps.reorder_to,
               (ps.on_hand - ps.reserved) AS available
        FROM part_stock ps
        JOIN store st ON st.id = ps.store_id
        LEFT JOIN datacenter dc ON dc.id = st.datacenter_id
        WHERE ps.part_id = CAST(:id AS uuid)
        ORDER BY st.name
    """), {"id": part_id})).mappings().all()
    return [dict(r) for r in rows]


async def set_reorder(session: AsyncSession, *, part_id: str, store_id: str,
                      reorder_at: int | None, reorder_to: int | None) -> None:
    """Reorder points are policy, not stock - the one thing on part_stock that
    is safe to set directly."""
    await session.execute(text("""
        INSERT INTO part_stock (part_id, store_id, reorder_at, reorder_to)
        VALUES (CAST(:part_id AS uuid), CAST(:store_id AS uuid), :at, :to)
        ON CONFLICT (part_id, store_id) DO UPDATE
            SET reorder_at = EXCLUDED.reorder_at,
                reorder_to = EXCLUDED.reorder_to,
                updated_at = now()
    """), {"part_id": part_id, "store_id": store_id,
           "at": reorder_at, "to": reorder_to})


# ------------------------------------------------------------------ ledger

async def move(session: AsyncSession, *, part_id: str, store_id: str, delta: int,
               reason: str, actor: str, device_id: str | None = None,
               record_id: str | None = None, note: str | None = None) -> str:
    """Post one movement and apply it to the balance, atomically.

    The row is locked before the check so two concurrent consumptions of the
    last unit cannot both pass it. Without the lock the CHECK constraint would
    catch it anyway - but as a constraint violation rather than a message
    naming the part and the count, which is not something to show an operator.
    """
    current = (await session.execute(text("""
        SELECT on_hand FROM part_stock
        WHERE part_id = CAST(:p AS uuid) AND store_id = CAST(:s AS uuid)
        FOR UPDATE
    """), {"p": part_id, "s": store_id})).scalar_one_or_none()
    have = current or 0

    if delta < 0 and have + delta < 0:
        sku = (await session.execute(
            text("SELECT sku FROM part WHERE id = CAST(:id AS uuid)"),
            {"id": part_id})).scalar_one_or_none() or part_id
        raise InsufficientStockError(sku, -delta, have)

    # Two statements, not an upsert that adds. PostgreSQL evaluates a CHECK
    # constraint against the row PROPOSED by the INSERT before it resolves the
    # conflict, so `VALUES (..., -3)` trips `on_hand >= 0` even when the DO
    # UPDATE branch would have landed on 7. Ensuring the row exists at zero and
    # then adding to it is checked once, on the value that actually results.
    await session.execute(text("""
        INSERT INTO part_stock (part_id, store_id, on_hand)
        VALUES (CAST(:p AS uuid), CAST(:s AS uuid), 0)
        ON CONFLICT (part_id, store_id) DO NOTHING
    """), {"p": part_id, "s": store_id})
    await session.execute(text("""
        UPDATE part_stock
           SET on_hand = on_hand + :delta, updated_at = now()
         WHERE part_id = CAST(:p AS uuid) AND store_id = CAST(:s AS uuid)
    """), {"p": part_id, "s": store_id, "delta": delta})

    return (await session.execute(text("""
        INSERT INTO stock_movement (part_id, store_id, delta, reason, device_id,
                                    record_id, actor, note)
        VALUES (CAST(:p AS uuid), CAST(:s AS uuid), :delta, :reason,
                CAST(:device_id AS uuid), CAST(:record_id AS uuid), :actor, :note)
        RETURNING id::text
    """), {"p": part_id, "s": store_id, "delta": delta, "reason": reason,
           "device_id": device_id, "record_id": record_id,
           "actor": actor, "note": note})).scalar_one()


async def movements(session: AsyncSession, part_id: str,
                    limit: int = 200) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT m.id::text, m.delta, m.reason, m.actor, m.ts, m.note,
               st.name AS store_name,
               m.device_id::text AS device_id, d.name AS device_name
        FROM stock_movement m
        JOIN store st ON st.id = m.store_id
        LEFT JOIN device d ON d.id = m.device_id
        WHERE m.part_id = CAST(:id AS uuid)
        ORDER BY m.ts DESC, m.id
        LIMIT :limit
    """), {"id": part_id, "limit": limit})).mappings().all()
    return [dict(r) for r in rows]


async def reconcile(session: AsyncSession) -> list[dict[str, Any]]:
    """Where the running total and the ledger disagree.

    They never should - `move` writes both in one transaction - so a non-empty
    result means something wrote `part_stock` outside this module, which is the
    failure the whole design exists to prevent. Cheap to check and worth a test.
    """
    rows = (await session.execute(text("""
        SELECT p.sku, st.name AS store_name, ps.on_hand,
               COALESCE(m.total, 0) AS ledger
        FROM part_stock ps
        JOIN part p ON p.id = ps.part_id
        JOIN store st ON st.id = ps.store_id
        LEFT JOIN (SELECT part_id, store_id, sum(delta) AS total
                   FROM stock_movement GROUP BY part_id, store_id) m
               ON m.part_id = ps.part_id AND m.store_id = ps.store_id
        WHERE ps.on_hand <> COALESCE(m.total, 0)
    """))).mappings().all()
    return [dict(r) for r in rows]


async def low_stock_count(session: AsyncSession) -> int:
    return (await session.execute(text("""
        SELECT count(*) FROM part_stock
        WHERE reorder_at IS NOT NULL AND on_hand <= reorder_at
    """))).scalar_one()
