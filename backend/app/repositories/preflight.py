"""collector_preflight_result - docs/26 Phase 8."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def record(session: AsyncSession, collector_id: str, passed: bool,
                 checks: list[dict[str, Any]]) -> str:
    return await session.scalar(text("""
        INSERT INTO collector_preflight_result (collector_id, passed, checks)
        VALUES (:id, :passed, CAST(:checks AS jsonb))
        RETURNING id::text
    """), {"id": collector_id, "passed": passed, "checks": json.dumps(checks)})


async def latest(session: AsyncSession, collector_id: str) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        SELECT id::text, collector_id, ran_at, passed, checks
          FROM collector_preflight_result
         WHERE collector_id = :id
         ORDER BY ran_at DESC
         LIMIT 1
    """), {"id": collector_id})).mappings().first()
    return dict(row) if row else None


async def history(session: AsyncSession, collector_id: str,
                  limit: int = 20) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT id::text, collector_id, ran_at, passed, checks
          FROM collector_preflight_result
         WHERE collector_id = :id
         ORDER BY ran_at DESC
         LIMIT :limit
    """), {"id": collector_id, "limit": limit})).mappings().all()
    return [dict(r) for r in rows]
