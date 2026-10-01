"""collector_command, collector_release, collector_rollout (docs/26 Phase 7,
migration 0095). Raw SQL like the rest of repositories/."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: Delivered but never answered within this long is expired, so a collector
#: that died mid-command does not hold its rollout forever. Longer than any
#: upgrade's own verify window plus a restart.
COMMAND_TIMEOUT_S = 900


# ------------------------------------------------------------------ commands

async def claim_pending(session: AsyncSession, collector_id: str) -> list[dict[str, Any]]:
    """Pending commands for this collector, marked delivered in the same
    statement - SKIP LOCKED, so two API workers answering two long-polls from
    one collector cannot both deliver the same command."""
    rows = (await session.execute(text("""
        UPDATE collector_command c SET state = 'delivered', delivered_at = now()
         WHERE c.id IN (SELECT id FROM collector_command
                         WHERE collector_id = :cid AND state = 'pending'
                         ORDER BY created_at FOR UPDATE SKIP LOCKED)
        RETURNING c.id::text, c.kind, c.payload, c.created_at
    """), {"cid": collector_id})).mappings().all()
    return [dict(r) for r in rows]


async def moves_token(session: AsyncSession) -> str:
    """Changes whenever any endpoint's owner or any endpoint row changes - the
    cheap "your assignment may have changed" signal a long-poll carries, so a
    collector refetches at once instead of on its next 30 s tick. A collector
    whose own share did not change gets a 304 for its trouble."""
    row = (await session.execute(text("""
        SELECT COALESCE(extract(epoch FROM (SELECT max(at) FROM endpoint_assignment_history)), 0)
                   ::numeric(20, 3)::text AS moved,
               COALESCE(extract(epoch FROM (SELECT max(updated_at) FROM device_endpoint)), 0)
                   ::numeric(20, 3)::text AS edited
    """))).mappings().one()
    return f"{row['moved']}/{row['edited']}"


async def create(session: AsyncSession, collector_id: str, kind: str,
                 payload: dict[str, Any], actor: str,
                 rollout_id: str | None = None) -> str:
    return await session.scalar(text("""
        INSERT INTO collector_command (collector_id, kind, payload, created_by, rollout_id)
        VALUES (:cid, :kind, CAST(:payload AS jsonb), :actor, CAST(:rid AS uuid))
        RETURNING id::text
    """), {"cid": collector_id, "kind": kind, "payload": json.dumps(payload),
           "actor": actor, "rid": rollout_id})


async def finish(session: AsyncSession, command_id: str, collector_id: str,
                 state: str, result: dict[str, Any]) -> bool:
    """Record a collector's answer. Only its own command, and only once."""
    res = await session.execute(text("""
        UPDATE collector_command SET state = :state, finished_at = now(),
                                     result = CAST(:result AS jsonb)
         WHERE id = CAST(:id AS uuid) AND collector_id = :cid
           AND state IN ('pending', 'delivered')
    """), {"id": command_id, "cid": collector_id, "state": state,
           "result": json.dumps(result)})
    return bool(res.rowcount)


async def expire_stale(session: AsyncSession) -> int:
    res = await session.execute(text("""
        UPDATE collector_command SET state = 'expired', finished_at = now(),
               result = jsonb_build_object('detail', 'no answer within the command timeout')
         WHERE state IN ('pending', 'delivered')
           AND created_at < now() - make_interval(secs => :t)
    """), {"t": COMMAND_TIMEOUT_S})
    return res.rowcount or 0


async def open_for(session: AsyncSession, collector_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT id::text, kind, payload, state, rollout_id::text, created_at, delivered_at
          FROM collector_command
         WHERE collector_id = :cid AND state IN ('pending', 'delivered')
    """), {"cid": collector_id})).mappings().all()
    return [dict(r) for r in rows]


async def recent(session: AsyncSession, limit: int = 100) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT id::text, collector_id, kind, payload, state, rollout_id::text, result,
               created_at, created_by, delivered_at, finished_at
          FROM collector_command ORDER BY created_at DESC LIMIT :n
    """), {"n": limit})).mappings().all()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ releases

async def add_release(session: AsyncSession, r: dict[str, Any]) -> bool:
    res = await session.execute(text("""
        INSERT INTO collector_release (version, sha256, signature, key_id, size_bytes,
                                       path, notes, created_by)
        VALUES (:version, :sha256, :signature, :key_id, :size_bytes, :path, :notes, :actor)
        ON CONFLICT (version) DO NOTHING
    """), r)
    return bool(res.rowcount)


async def get_release(session: AsyncSession, version: str) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        SELECT version, sha256, signature, key_id, size_bytes, path, notes,
               created_at, created_by
          FROM collector_release WHERE version = :v
    """), {"v": version})).mappings().first()
    return dict(row) if row else None


async def releases(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT version, sha256, key_id, size_bytes, notes, created_at, created_by
          FROM collector_release ORDER BY created_at DESC
    """))).mappings().all()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ rollouts

async def create_rollout(session: AsyncSession, version: str, pool_ids: list[str],
                         actor: str) -> str:
    return await session.scalar(text("""
        INSERT INTO collector_rollout (version, pool_ids, created_by)
        VALUES (:v, CAST(:pools AS uuid[]), :actor) RETURNING id::text
    """), {"v": version, "pools": pool_ids, "actor": actor})


async def rollouts(session: AsyncSession, running_only: bool = False) -> list[dict[str, Any]]:
    rows = (await session.execute(text(f"""
        SELECT id::text, version, pool_ids::text[] AS pool_ids, state, detail,
               created_at, created_by, finished_at
          FROM collector_rollout
         {"WHERE state = 'running'" if running_only else ""}
         ORDER BY created_at DESC LIMIT 50
    """))).mappings().all()
    return [dict(r) for r in rows]


async def set_rollout_state(session: AsyncSession, rollout_id: str, state: str,
                            detail: str | None = None) -> None:
    await session.execute(text("""
        UPDATE collector_rollout
           SET state = :s, detail = COALESCE(:d, detail),
               finished_at = CASE WHEN :s IN ('succeeded','failed','cancelled')
                                  THEN now() ELSE finished_at END
         WHERE id = CAST(:id AS uuid)
    """), {"id": rollout_id, "s": state, "d": detail})


async def rollout_commands(session: AsyncSession, rollout_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT id::text, collector_id, state, result, created_at, finished_at
          FROM collector_command WHERE rollout_id = CAST(:id AS uuid)
         ORDER BY created_at
    """), {"id": rollout_id})).mappings().all()
    return [dict(r) for r in rows]
