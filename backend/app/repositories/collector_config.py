"""Stored collector configuration, and what each collector reports running."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def get(session: AsyncSession, collector_id: str) -> dict[str, Any]:
    """The stored overrides, or an empty document for a collector with none.

    Absent is not an error: a collector that has never been configured from
    here runs its file, which is the correct behaviour and the state every
    installation starts in.
    """
    row = (await session.execute(text("""
        SELECT collector_id, config, version, updated_at, updated_by
          FROM collector_config
         WHERE collector_id = CAST(:id AS text)
    """), {"id": collector_id})).mappings().first()
    if row is None:
        return {"collector_id": collector_id, "config": {}, "version": 0,
                "updated_at": None, "updated_by": None}
    return dict(row)


async def put(session: AsyncSession, collector_id: str, config: dict[str, Any],
              actor: str) -> dict[str, Any]:
    """Replace the document and bump the version.

    A whole-document write rather than a merge: the page always sends the
    complete set it is showing, and a merge would make "clear this override"
    unexpressible - the absence of a key would mean "leave it" instead of
    "fall back to the file".
    """
    row = (await session.execute(text("""
        INSERT INTO collector_config (collector_id, config, version, updated_by)
        VALUES (CAST(:id AS text), CAST(:config AS jsonb), 1, CAST(:actor AS text))
        ON CONFLICT (collector_id) DO UPDATE
           SET config = EXCLUDED.config,
               version = collector_config.version + 1,
               updated_at = now(),
               updated_by = EXCLUDED.updated_by
        RETURNING collector_id, config, version, updated_at, updated_by
    """), {"id": collector_id, "config": json.dumps(config),
           "actor": actor})).mappings().one()
    return dict(row)


async def list_collectors(session: AsyncSession) -> list[dict[str, Any]]:
    """Every collector this platform has heard from, with both configurations.

    `version` is what is stored; `running_version` is what the collector's last
    heartbeat said it is actually running. Showing one without the other is how
    a settings page comes to report a change that never reached anything.
    """
    rows = (await session.execute(text("""
        SELECT ci.id, ci.hostname, ci.version AS build, ci.status,
               ci.started_at, ci.last_heartbeat,
               ci.endpoints_owned, ci.endpoints_online,
               coalesce(cc.config, '{}'::jsonb)     AS config,
               coalesce(cc.version, 0)              AS version,
               cc.updated_at, cc.updated_by,
               coalesce((ci.stats->>'config_version')::int, 0)
                                                    AS running_version,
               coalesce((ci.stats->>'config_restart_pending')::boolean, false)
                                                    AS restart_pending,
               nullif(ci.stats->>'config_error', '') AS config_error,
               (ci.last_heartbeat > now() - interval '90 seconds') AS alive
          FROM collector_instance ci
          LEFT JOIN collector_config cc ON cc.collector_id = ci.id
         ORDER BY ci.id
    """))).mappings().all()
    return [dict(r) for r in rows]
