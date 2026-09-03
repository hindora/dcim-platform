"""Stock movements, and consuming parts against maintenance."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import parts as repo
from app.repositories.parts import InsufficientStockError

__all__ = ["InsufficientStockError", "consume_for_record", "summary"]


async def consume_for_record(session: AsyncSession, *, record_id: str,
                             device_id: str, parts_used: list[dict[str, Any]],
                             actor: str) -> int:
    """Post one movement per line on a maintenance record.

    All or nothing. A record that claims two PSUs were fitted while only one
    came out of stock describes work that did not happen the way it is written
    down - so an insufficient line fails the whole record rather than leaving a
    half-true one behind. The caller's transaction does the rolling back.
    """
    posted = 0
    for line in parts_used:
        part_id = line.get("part_id")
        store_id = line.get("store_id")
        qty = int(line.get("quantity") or 0)
        if not part_id or not store_id or qty <= 0:
            continue
        await repo.move(session, part_id=part_id, store_id=store_id,
                        delta=-qty, reason="consumed", actor=actor,
                        device_id=device_id, record_id=record_id,
                        note=line.get("note"))
        posted += 1
    return posted


def normalise_lines(parts_used: list[dict[str, Any]]) -> str:
    """What gets stored on the record itself.

    The movements are the truth; this is the human-readable copy that survives a
    part later being deleted from the catalog.
    """
    return json.dumps(parts_used)


async def summary(session: AsyncSession) -> dict[str, Any]:
    return {
        "parts_below_reorder": await repo.low_stock_count(session),
    }
