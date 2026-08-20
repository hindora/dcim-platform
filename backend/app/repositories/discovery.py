"""Discovery runs and candidate staging."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def create_run(session: AsyncSession, *, method: str,
                     scope: dict[str, Any]) -> dict[str, Any]:
    row = (await session.execute(text("""
        INSERT INTO discovery_run (method, scope, status)
        VALUES (:method, CAST(:scope AS jsonb), 'pending')
        RETURNING id::text, method, scope, status, started_at
    """), {"method": method, "scope": json.dumps(scope)})).mappings().first()
    return dict(row)


async def list_runs(session: AsyncSession, limit: int = 25) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT id::text, method, scope, status, found, promoted,
               started_at, finished_at, error
          FROM discovery_run ORDER BY started_at DESC LIMIT :limit
    """), {"limit": limit})).mappings().all()
    return [dict(r) for r in rows]


async def claim_pending(session: AsyncSession) -> dict[str, Any] | None:
    """Hand the oldest pending run to a collector, exactly once.

    SKIP LOCKED so two collectors cannot claim the same run: the second skips
    it rather than blocking, which is what you want when the work is a network
    sweep that must not be done twice.
    """
    row = (await session.execute(text("""
        UPDATE discovery_run SET status = 'running'
         WHERE id = (SELECT id FROM discovery_run
                      WHERE status = 'pending'
                      ORDER BY started_at
                      FOR UPDATE SKIP LOCKED
                      LIMIT 1)
        RETURNING id::text, method, scope
    """))).mappings().first()
    return dict(row) if row else None


async def finish_run(session: AsyncSession, run_id: str, *, found: int,
                     status: str = "done", error: str | None = None) -> None:
    await session.execute(text("""
        UPDATE discovery_run
           SET status = :status, finished_at = now(), found = :found, error = :error
         WHERE id = CAST(:id AS uuid)
    """), {"id": run_id, "status": status, "found": found, "error": error})


async def match_addresses(session: AsyncSession,
                          addresses: list[str]) -> dict[str, dict[str, Any]]:
    """Which of these addresses inventory already knows.

    Checked against BOTH the device's management address and the addresses its
    endpoints are polled on. A device is frequently managed on one address and
    polled on another; matching on only one of them reports half the fleet as
    unmanaged, which is the fastest way to make an audit useless.
    """
    if not addresses:
        return {}
    rows = (await session.execute(text("""
        SELECT host(d.mgmt_ip) AS addr, d.id::text AS device_id, d.name
          FROM device d
         WHERE d.mgmt_ip IS NOT NULL
           AND host(d.mgmt_ip) = ANY(:addrs)
           AND d.lifecycle <> 'decommissioned'
        UNION
        SELECT host(e.address) AS addr, d.id::text AS device_id, d.name
          FROM device_endpoint e
          JOIN device d ON d.id = e.device_id
         WHERE e.address IS NOT NULL
           AND host(e.address) = ANY(:addrs)
           AND d.lifecycle <> 'decommissioned'
    """), {"addrs": addresses})).mappings().all()
    return {r["addr"]: {"device_id": r["device_id"], "name": r["name"]}
            for r in rows}


async def upsert_candidate(session: AsyncSession, *, run_id: str, address: str,
                           protocol: str, identity: dict[str, Any],
                           matched_device_id: str | None,
                           suggested_device_type: str | None = None,
                           suggested_vendor: str | None = None) -> str | None:
    """Record one responder.

    Conflicts on the open-candidate index, so rediscovering the same unmanaged
    address updates last_seen instead of growing a new row every sweep.
    """
    row = (await session.execute(text("""
        INSERT INTO discovery_candidate
               (run_id, address, protocol, identity, matched_device_id,
                suggested_device_type, suggested_vendor, status)
        VALUES (CAST(:run_id AS uuid), CAST(:address AS inet),
                CAST(:protocol AS protocol_t), CAST(:identity AS jsonb),
                CAST(:matched AS uuid), :dtype, :vendor, 'new')
        ON CONFLICT (address, protocol) WHERE status = 'new'
        DO UPDATE SET last_seen = now(),
                      run_id = EXCLUDED.run_id,
                      identity = EXCLUDED.identity,
                      matched_device_id = EXCLUDED.matched_device_id
        RETURNING id::text
    """), {"run_id": run_id, "address": address, "protocol": protocol,
           "identity": json.dumps(identity), "matched": matched_device_id,
           "dtype": suggested_device_type, "vendor": suggested_vendor})
    ).mappings().first()
    return row["id"] if row else None


async def list_candidates(session: AsyncSession, *, run_id: str | None = None,
                          status: str | None = None,
                          unmatched_only: bool = False,
                          limit: int = 200) -> list[dict[str, Any]]:
    where = ["1=1"]
    params: dict[str, Any] = {"limit": limit}
    if run_id:
        where.append("c.run_id = CAST(:run_id AS uuid)")
        params["run_id"] = run_id
    if status:
        where.append("c.status = :status")
        params["status"] = status
    if unmatched_only:
        where.append("c.matched_device_id IS NULL")

    rows = (await session.execute(text(f"""
        SELECT c.id::text, c.run_id::text AS run_id, host(c.address) AS address,
               c.protocol::text AS protocol, c.identity,
               c.suggested_device_type, c.suggested_vendor, c.suggested_model,
               c.matched_device_id::text AS matched_device_id,
               d.name AS matched_device_name,
               c.status, c.first_seen, c.last_seen
          FROM discovery_candidate c
          LEFT JOIN device d ON d.id = c.matched_device_id
         WHERE {' AND '.join(where)}
         ORDER BY (c.matched_device_id IS NULL) DESC, c.address
         LIMIT :limit
    """), params)).mappings().all()
    return [dict(r) for r in rows]


async def set_candidate_status(session: AsyncSession, candidate_id: str,
                               status: str) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        UPDATE discovery_candidate SET status = :status
         WHERE id = CAST(:id AS uuid)
        RETURNING id::text, host(address) AS address, protocol::text AS protocol,
                  identity, status
    """), {"id": candidate_id, "status": status})).mappings().first()
    return dict(row) if row else None


async def get_candidate(session: AsyncSession,
                        candidate_id: str) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        SELECT id::text, run_id::text AS run_id, host(address) AS address,
               protocol::text AS protocol, identity, suggested_device_type,
               suggested_vendor, matched_device_id::text AS matched_device_id,
               status
          FROM discovery_candidate WHERE id = CAST(:id AS uuid)
    """), {"id": candidate_id})).mappings().first()
    return dict(row) if row else None
