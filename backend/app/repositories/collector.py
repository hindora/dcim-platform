"""Collector assignment queries.

The DCIM database is the source of truth for what exists; the collector pulls
its work list from here rather than reading a static file, because the fleet
changes at runtime and a file goes stale within minutes.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def register_collector(session: AsyncSession, collector_id: str) -> bool:
    """Record that a collector exists, if this is the first anyone has heard.

    Asking for work is an announcement. Without it there is a window between a
    new collector's first assignment fetch and its first heartbeat in which the
    other collectors do not know it exists and therefore do not yield its
    share - so both poll the same endpoints, which is exactly the overlap
    sharding is for.

    DO NOTHING on conflict, deliberately: an existing collector's liveness is
    owned by its heartbeat, and refreshing last_heartbeat here would make a
    collector that fetches assignments but has stopped polling look healthy.

    The first collector ever is active at once - the single-collector install
    has nobody to approve it. Every later one that nobody created in advance
    arrives PENDING and takes no work until an admin places and approves it:
    an unknown id is a typo in a config file as often as it is a new machine,
    and either way it must not take a share of every site on its first fetch.
    """
    result = await session.execute(text("""
        INSERT INTO collector_instance (id, last_heartbeat, endpoints_owned,
                                        endpoints_online, status, stats, state)
        VALUES (:id, now(), 0, 0, 'UNKNOWN', '{}'::jsonb,
                CASE WHEN EXISTS (SELECT 1 FROM collector_instance
                                   WHERE state <> 'decommissioned')
                     THEN 'pending' ELSE 'active' END)
        ON CONFLICT (id) DO NOTHING
    """), {"id": collector_id})
    return bool(result.rowcount)


async def live_collectors(session: AsyncSession,
                          stale_after_s: float = 60.0) -> list[dict[str, Any]]:
    """Every collector that may own endpoints, with the site it is placed in.

    Registration is the heartbeat: a collector that has ever checked in has a
    row. Stale ones are returned too, flagged rather than dropped, because
    removing a collector from the set silently reassigns its whole shard - see
    services/sharding.py on why that is a decision and not a default. Retired
    ones are not: a decommissioned collector owns nothing, and leaving it in
    the set would keep its share of the hash pointing at a machine that is gone.

    The site comes from the admin's placement (``datacenter_id``), not from
    anything the collector says about itself. It used to be read from
    ``stats.sites``, which no heartbeat ever carried - so every collector was
    eligible for every site and a second one was handed half of a datacenter
    it cannot route to.
    """
    rows = (await session.execute(text("""
        SELECT ci.id, ci.status, ci.state, dc.code AS site,
               ci.started_at IS NOT NULL AS has_run,
               extract(epoch FROM (clock_timestamp() - ci.last_heartbeat)) AS age_s
          FROM collector_instance ci
          LEFT JOIN datacenter dc ON dc.id = ci.datacenter_id
         WHERE ci.state NOT IN ('decommissioned', 'pending')
         ORDER BY ci.id
    """))).mappings().all()
    out = []
    for r in rows:
        age = float(r["age_s"]) if r["age_s"] is not None else None
        out.append({
            "collector_id": r["id"],
            # Absent means "serves any site", which is the correct default for
            # a single-site deployment.
            "sites": [r["site"]] if r["site"] else [],
            "healthy": age is not None and age < stale_after_s,
            # Takes a share of the hash only once it has actually run. A
            # collector created ahead of its install would otherwise pull its
            # site's share away from the collector polling it today, and that
            # share would sit unpolled until somebody got round to the VM.
            # The assignment it fetches before its first heartbeat is empty;
            # the next one, thirty seconds later, is not.
            "accepting": r["state"] == "active" and bool(r["has_run"]),
            "heartbeat_age_s": age,
        })
    return out


async def collector_state(session: AsyncSession,
                          collector_id: str) -> dict[str, Any] | None:
    """The admin-held facts about one collector: its intent and its token."""
    row = (await session.execute(text("""
        SELECT id, state, token_generation, datacenter_id::text AS datacenter_id
          FROM collector_instance WHERE id = :id
    """), {"id": collector_id})).mappings().first()
    return dict(row) if row else None


async def encryption_pubkey(session: AsyncSession, collector_id: str) -> str | None:
    """This collector's registered X25519 public key (base64), or None if it
    has never enrolled with one - docs/26 Phase 4. build_assignment uses
    this to decide whether a credential is sealed or returned plaintext."""
    row = (await session.execute(text("""
        SELECT encryption_pubkey FROM collector_instance WHERE id = :id
    """), {"id": collector_id})).first()
    return row[0] if row and row[0] else None


async def create_collector(session: AsyncSession, collector_id: str,
                           datacenter_id: str | None, actor: str) -> bool:
    """Create a collector before it first runs, already placed and approved.

    The way a second collector should arrive: known, sited and with its own
    token before it ever asks for work. Generation 1, so no token derived under
    the original scheme - generation 0 - is valid for it.
    """
    result = await session.execute(text("""
        INSERT INTO collector_instance (id, last_heartbeat, endpoints_owned,
                                        endpoints_online, status, stats, state,
                                        datacenter_id, token_generation,
                                        state_changed_at, state_changed_by)
        VALUES (:id, now(), 0, 0, 'UNKNOWN', '{}'::jsonb, 'active',
                CAST(:dc AS uuid), 1, now(), :actor)
        ON CONFLICT (id) DO NOTHING
    """), {"id": collector_id, "dc": datacenter_id, "actor": actor})
    return bool(result.rowcount)


async def set_placement(session: AsyncSession, collector_id: str,
                        datacenter_id: str | None) -> None:
    await session.execute(text("""
        UPDATE collector_instance SET datacenter_id = CAST(:dc AS uuid)
         WHERE id = :id
    """), {"id": collector_id, "dc": datacenter_id})


async def set_state(session: AsyncSession, collector_id: str, state: str,
                    actor: str) -> None:
    await session.execute(text("""
        UPDATE collector_instance
           SET state = :state, state_changed_at = now(), state_changed_by = :actor
         WHERE id = :id
    """), {"id": collector_id, "state": state, "actor": actor})


async def bump_token_generation(session: AsyncSession, collector_id: str) -> int:
    """Revoke every token this collector holds; return the generation now honoured."""
    return int((await session.execute(text("""
        UPDATE collector_instance SET token_generation = token_generation + 1
         WHERE id = :id RETURNING token_generation
    """), {"id": collector_id})).scalar_one())


async def unpin_all(session: AsyncSession, collector_id: str) -> int:
    """Release every endpoint pinned to a collector back to the hash.

    Bumps updated_at, because that is what tells the other collectors their
    assignment changed.
    """
    result = await session.execute(text("""
        UPDATE device_endpoint SET collector_id = NULL, updated_at = now()
         WHERE collector_id = :id
    """), {"id": collector_id})
    return int(result.rowcount or 0)


async def pinned_count(session: AsyncSession, collector_id: str) -> int:
    return int((await session.execute(text("""
        SELECT count(*) FROM device_endpoint WHERE collector_id = :id
    """), {"id": collector_id})).scalar() or 0)


async def assignment_endpoints(session: AsyncSession, collector_id: str,
                               protocols: list[str] | None = None) -> list[dict[str, Any]]:
    """Endpoints this collector owns.

    ``collector_id IS NULL`` means unsharded - any collector may take it. With
    more than one collector, set the column and the split becomes explicit.
    """
    # Candidates, not "mine": unpinned endpoints plus the ones pinned to this
    # collector. Which of the unpinned ones this collector actually owns is
    # decided by services/sharding.py - the old predicate handed every
    # unpinned endpoint to every collector, which is a complete overlap the
    # moment a second one exists.
    where = ["e.enabled", "e.admin_state = 'enabled'",
             "d.lifecycle <> 'decommissioned'",
             "(e.collector_id IS NULL OR e.collector_id = :collector_id)"]
    params: dict[str, Any] = {"collector_id": collector_id}
    if protocols:
        where.append("e.protocol::text = ANY(:protocols)")
        params["protocols"] = protocols

    rows = (await session.execute(text(f"""
        SELECT e.id::text, e.device_id::text, d.name AS device_name, d.device_type,
               v.name AS vendor, m.name AS model,
               e.protocol::text AS protocol, e.role::text AS role,
               host(e.address) AS address, e.port, e.addressing,
               e.via_endpoint_id::text,
               c.kind AS credential_kind, c.secret_enc,
               e.collector_id,
               dc.code AS site,
               p.interval_s, p.timeout_ms, p.retries, p.metric_groups, p.push_enabled
        FROM device_endpoint e
        JOIN device d        ON d.id = e.device_id
        JOIN poll_profile p  ON p.id = e.poll_profile_id
        LEFT JOIN rack rk    ON rk.id = d.rack_id
        LEFT JOIN rack_row rr ON rr.id = rk.row_id
        LEFT JOIN room rm    ON rm.id = COALESCE(rr.room_id, d.room_id)
        LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
        LEFT JOIN vendor v   ON v.id = d.vendor_id
        LEFT JOIN model m    ON m.id = d.model_id
        LEFT JOIN credential c ON c.id = e.credential_id
        WHERE {' AND '.join(where)}
        ORDER BY e.id
    """), params)).mappings().all()
    return [dict(r) for r in rows]


async def ownable_endpoints(session: AsyncSession) -> list[dict[str, Any]]:
    """Every endpoint some collector should poll, with its site and pin.

    The same predicate as ``assignment_endpoints`` but for the whole fleet and
    only the three columns sharding reads: the plan over all of it is what the
    visibility sweep and the ingest ownership check need, and neither has any
    business decrypting a credential to get it.
    """
    rows = (await session.execute(text("""
        SELECT e.id::text, e.collector_id, dc.code AS site
        FROM device_endpoint e
        JOIN device d        ON d.id = e.device_id
        JOIN poll_profile p  ON p.id = e.poll_profile_id
        LEFT JOIN rack rk    ON rk.id = d.rack_id
        LEFT JOIN rack_row rr ON rr.id = rk.row_id
        LEFT JOIN room rm    ON rm.id = COALESCE(rr.room_id, d.room_id)
        LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
        WHERE e.enabled AND e.admin_state = 'enabled'
          AND d.lifecycle <> 'decommissioned'
    """))).mappings().all()
    return [dict(r) for r in rows]


async def resolvable_endpoints(session: AsyncSession) -> list[dict[str, Any]]:
    """Every endpoint a trap or event could arrive from, whoever owns it.

    The trap resolver's view, not a work list: no poll settings, and the
    credential is returned only so the service can digest a community out of
    it - it is never served.
    """
    rows = (await session.execute(text("""
        SELECT e.id::text, e.device_id::text, d.name AS device_name, d.device_type,
               e.protocol::text AS protocol, e.role::text AS role,
               host(e.address) AS address, dc.code AS site,
               c.kind AS credential_kind, c.secret_enc
        FROM device_endpoint e
        JOIN device d        ON d.id = e.device_id
        LEFT JOIN rack rk    ON rk.id = d.rack_id
        LEFT JOIN rack_row rr ON rr.id = rk.row_id
        LEFT JOIN room rm    ON rm.id = COALESCE(rr.room_id, d.room_id)
        LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
        LEFT JOIN credential c ON c.id = e.credential_id
        WHERE e.enabled AND e.admin_state = 'enabled'
          AND d.lifecycle <> 'decommissioned'
          AND e.protocol::text IN ('snmp', 'redfish')
        ORDER BY e.id
    """))).mappings().all()
    return [dict(r) for r in rows]


async def assignment_version(session: AsyncSession, collector_id: str) -> int:
    """A cheap version number for ETag purposes.

    Derived from the newest endpoint mutation rather than a counter table: any
    insert, update or soft delete moves ``updated_at``, so the ETag changes
    exactly when the assignment content changes.
    """
    row = (await session.execute(text("""
        SELECT count(*) AS n,
               COALESCE(extract(epoch FROM max(e.updated_at))::bigint, 0) AS newest
        FROM device_endpoint e
        JOIN device d ON d.id = e.device_id
        WHERE e.enabled AND d.lifecycle <> 'decommissioned'
          AND (e.collector_id IS NULL OR e.collector_id = :collector_id)
    """), {"collector_id": collector_id})).mappings().first()
    if not row:
        return 0
    # Combine count and newest mtime: a delete lowers the count even when no
    # timestamp advances.
    return int(row["newest"]) * 100_000 + int(row["n"])


async def upsert_heartbeat(session: AsyncSession, hb: dict[str, Any]) -> None:
    await session.execute(text("""
        INSERT INTO collector_instance (id, version, hostname, started_at,
                                        last_heartbeat, endpoints_owned,
                                        endpoints_online, status, stats, state)
        VALUES (:id, :version, :hostname, :started_at, now(),
                :endpoints_owned, :endpoints_online, 'HEALTHY', CAST(:stats AS jsonb),
                CASE WHEN EXISTS (SELECT 1 FROM collector_instance
                                   WHERE state <> 'decommissioned')
                     THEN 'pending' ELSE 'active' END)
        ON CONFLICT (id) DO UPDATE SET
            version = EXCLUDED.version,
            hostname = EXCLUDED.hostname,
            last_heartbeat = now(),
            endpoints_owned = EXCLUDED.endpoints_owned,
            endpoints_online = EXCLUDED.endpoints_online,
            status = 'HEALTHY',
            stats = EXCLUDED.stats
    """), hb)
