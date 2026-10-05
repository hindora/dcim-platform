"""Collector assignment queries.

The DCIM database is the source of truth for what exists; the collector pulls
its work list from here rather than reading a static file, because the fleet
changes at runtime and a file goes stale within minutes.
"""

from __future__ import annotations

import json
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
        SELECT ci.id, ci.status, ci.state, dc.code AS site, ci.pool_id::text AS pool_id,
               ci.started_at IS NOT NULL AS has_run,
               extract(epoch FROM (clock_timestamp() - ci.last_heartbeat)) AS age_s,
               extract(epoch FROM (clock_timestamp() - ci.healthy_since))
                   AS healthy_duration_s
          FROM collector_instance ci
          LEFT JOIN datacenter dc ON dc.id = ci.datacenter_id
         WHERE ci.state NOT IN ('decommissioned', 'pending')
         ORDER BY ci.id
    """))).mappings().all()
    out = []
    for r in rows:
        age = float(r["age_s"]) if r["age_s"] is not None else None
        healthy_duration = (float(r["healthy_duration_s"])
                           if r["healthy_duration_s"] is not None else None)
        out.append({
            "collector_id": r["id"],
            # Absent means "serves any site", which is the correct default for
            # a single-site deployment.
            "sites": [r["site"]] if r["site"] else [],
            # docs/26 Phase 5: set, this REPLACES sites as the whole placement
            # decision (see services/sharding.Collector.serves) - a collector
            # an admin has placed in a pool serves that pool and nothing else.
            "pool_id": r["pool_id"],
            "healthy": age is not None and age < stale_after_s,
            # Takes a share of the hash only once it has actually run. A
            # collector created ahead of its install would otherwise pull its
            # site's share away from the collector polling it today, and that
            # share would sit unpolled until somebody got round to the VM.
            # The assignment it fetches before its first heartbeat is empty;
            # the next one, thirty seconds later, is not.
            "accepting": r["state"] == "active" and bool(r["has_run"]),
            "heartbeat_age_s": age,
            # docs/26 Phase 6: how long this collector has been continuously
            # healthy, not just whether it is healthy right now - what
            # failback damping measures its 10-minute window from. None for
            # a collector that has never sent a heartbeat with the column
            # populated (a row from before migration 0089, or one that has
            # never heartbeated at all).
            "healthy_duration_s": healthy_duration,
        })
    return out


async def collector_state(session: AsyncSession,
                          collector_id: str) -> dict[str, Any] | None:
    """The admin-held facts about one collector: its intent and its token."""
    row = (await session.execute(text("""
        SELECT id, state, token_generation, datacenter_id::text AS datacenter_id,
               pool_id::text AS pool_id
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
                           datacenter_id: str | None, actor: str,
                           pool_id: str | None = None) -> bool:
    """Create a collector before it first runs, already placed and approved.

    The way a second collector should arrive: known, sited and with its own
    token before it ever asks for work. Generation 1, so no token derived under
    the original scheme - generation 0 - is valid for it.
    """
    result = await session.execute(text("""
        INSERT INTO collector_instance (id, last_heartbeat, endpoints_owned,
                                        endpoints_online, status, stats, state,
                                        datacenter_id, pool_id, token_generation,
                                        state_changed_at, state_changed_by)
        VALUES (:id, now(), 0, 0, 'UNKNOWN', '{}'::jsonb, 'active',
                CAST(:dc AS uuid), CAST(:pool AS uuid), 1, now(), :actor)
        ON CONFLICT (id) DO NOTHING
    """), {"id": collector_id, "dc": datacenter_id, "pool": pool_id, "actor": actor})
    return bool(result.rowcount)


async def set_placement(session: AsyncSession, collector_id: str,
                        datacenter_id: str | None) -> None:
    await session.execute(text("""
        UPDATE collector_instance SET datacenter_id = CAST(:dc AS uuid)
         WHERE id = :id
    """), {"id": collector_id, "dc": datacenter_id})


async def set_pool(session: AsyncSession, collector_id: str,
                   pool_id: str | None) -> None:
    """docs/26 Phase 5: a pool is the WHOLE placement once set (see
    services/sharding.Collector.serves) - it replaces datacenter_id as the
    decision, so the site is kept in step with the pool's own site rather
    than left saying something the pool contradicts."""
    await session.execute(text("""
        UPDATE collector_instance
           SET pool_id = CAST(:pool AS uuid),
               datacenter_id = COALESCE(
                   (SELECT datacenter_id FROM collector_pool
                     WHERE id = CAST(:pool AS uuid)), datacenter_id)
         WHERE id = :id
    """), {"id": collector_id, "pool": pool_id})


async def endpoint_health(session: AsyncSession,
                          endpoint_ids: list[str]) -> list[dict[str, Any]]:
    """What a collector detail page shows per owned endpoint (docs/26
    Phase 8): the device, how it is reached, and its last reported state.
    Credential-free - this page is readable by any signed-in user."""
    if not endpoint_ids:
        return []
    rows = (await session.execute(text("""
        SELECT e.id::text, e.device_id::text, d.name AS device_name, d.device_type,
               e.protocol::text AS protocol, host(e.address) AS address, e.port,
               COALESCE(es.status::text, 'UNKNOWN') AS status,
               es.last_seen, es.last_success, es.last_failure,
               es.last_error, es.last_error_class,
               COALESCE(es.consecutive_failures, 0) AS consecutive_failures,
               es.last_latency_ms, es.collector_id AS reported_by
          FROM device_endpoint e
          JOIN device d ON d.id = e.device_id
          LEFT JOIN endpoint_state es ON es.endpoint_id = e.id
         WHERE e.id = ANY(CAST(:ids AS uuid[]))
         ORDER BY d.name, e.protocol
    """), {"ids": endpoint_ids})).mappings().all()
    return [dict(r) for r in rows]


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
               host(e.address) AS address, e.port, e.addressing, e.target_limit,
               e.via_endpoint_id::text,
               c.kind AS credential_kind, c.secret_enc, c.key_id AS credential_key_id,
               e.collector_id,
               dc.code AS site,
               rp.id::text AS pool_id,
               (e.credential_id IS NULL AND pc.credential_id IS NOT NULL) AS credential_from_pool,
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
        -- The endpoint's pool, resolved once (the same rule as
        -- repositories/pools._RESOLVED_POOL), so its default credential for
        -- this protocol can stand in when the endpoint has none of its own
        -- (docs/26 Phase 4 credential sets, migration 0097).
        LEFT JOIN LATERAL (SELECT COALESCE(e.pool_id, (
                   SELECT cp.id
                     FROM discovery_range dr
                     JOIN collector_pool cp
                       ON cp.datacenter_id = dc.id AND cp.plane = dr.purpose
                    WHERE e.address IS NOT NULL AND e.address <<= dr.cidr
                    ORDER BY masklen(dr.cidr) DESC
                    LIMIT 1
               )) AS id) rp ON true
        LEFT JOIN collector_pool_credential pc
               ON pc.pool_id = rp.id AND pc.protocol = e.protocol::text
        LEFT JOIN credential c ON c.id = COALESCE(e.credential_id, pc.credential_id)
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
        SELECT e.id::text, e.collector_id, dc.code AS site,
               COALESCE(e.pool_id, (
                   SELECT cp.id
                     FROM discovery_range dr
                     JOIN collector_pool cp
                       ON cp.datacenter_id = dc.id AND cp.plane = dr.purpose
                    WHERE e.address IS NOT NULL AND e.address <<= dr.cidr
                    ORDER BY masklen(dr.cidr) DESC
                    LIMIT 1
               ))::text AS pool_id
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


async def pools(session: AsyncSession) -> list[dict[str, Any]]:
    """Every collector_pool row - docs/26 Phase 6 reads min_members (for
    apply_ha_policy) and datacenter_id (to match against an active
    blackout) from here; Phase 5 itself never needed this, only the
    per-endpoint pool_id resolution in assignment_endpoints/
    ownable_endpoints."""
    rows = (await session.execute(text("""
        SELECT id::text, datacenter_id::text, plane, min_members
          FROM collector_pool
    """))).mappings().all()
    return [dict(r) for r in rows]


async def current_assignment(session: AsyncSession) -> dict[str, str | None]:
    """What endpoint_assignment holds right now - docs/26 Phase 5's
    persisted plan, read by the assigner as its damping baseline and by
    build_assignment to serve a collector without recomputing anything."""
    rows = (await session.execute(text(
        "SELECT endpoint_id::text, collector_id FROM endpoint_assignment"
    ))).all()
    return {str(r[0]): r[1] for r in rows}


async def current_reasons(session: AsyncSession) -> dict[str, str]:
    """Why each endpoint holds its current owner - the assigner needs it to
    tell a failback (moving off a failover holder) from a rebalance."""
    rows = (await session.execute(text(
        "SELECT endpoint_id::text, reason FROM endpoint_assignment"
    ))).all()
    return {str(r[0]): r[1] for r in rows}


async def write_assignment(session: AsyncSession,
                           changes: dict[str, tuple[str | None, str]]) -> None:
    """Upserts endpoint_assignment for every endpoint whose owner actually
    changed - bumping epoch and since, recording reason - and writes one
    endpoint_assignment_history row per move (migration 0093) in the SAME
    statement, so a move and its record cannot disagree. Only called with
    real changes; a no-op tick writes nothing at all.

    `old` reads the pre-update owner: every sub-statement of one WITH sees
    the same snapshot, so it is the row as it was before `up` changed it."""
    if not changes:
        return
    await session.execute(text("""
        WITH v AS (
            SELECT (x->>'endpoint_id')::uuid AS endpoint_id,
                   x->>'collector_id' AS collector_id, x->>'reason' AS reason
              FROM jsonb_array_elements(CAST(:rows AS jsonb)) AS x
        ), old AS (
            SELECT ea.endpoint_id, ea.collector_id
              FROM endpoint_assignment ea JOIN v USING (endpoint_id)
        ), up AS (
            INSERT INTO endpoint_assignment (endpoint_id, collector_id, epoch, since, reason)
            SELECT endpoint_id, collector_id, 1, now(), reason FROM v
            ON CONFLICT (endpoint_id) DO UPDATE SET
                collector_id = EXCLUDED.collector_id,
                epoch = endpoint_assignment.epoch + 1,
                since = now(),
                reason = EXCLUDED.reason
            RETURNING endpoint_id, collector_id, epoch, reason
        )
        INSERT INTO endpoint_assignment_history
               (endpoint_id, from_collector, to_collector, epoch, reason)
        SELECT up.endpoint_id, old.collector_id, up.collector_id, up.epoch, up.reason
          FROM up LEFT JOIN old USING (endpoint_id)
    """), {"rows": json.dumps([
        {"endpoint_id": eid, "collector_id": owner, "reason": reason}
        for eid, (owner, reason) in changes.items()
    ])})


#: How long a move is kept. The plan's audit question - "who polled this UPS
#: at 03:12?" - is asked about incidents, which are reviewed within weeks and
#: occasionally within a quarter; half a year covers that with room. A
#: judgment, not a sourced figure: no DCIM vendor publishes one.
HISTORY_KEEP_DAYS = 180


async def prune_assignment_history(session: AsyncSession) -> int:
    result = await session.execute(text("""
        DELETE FROM endpoint_assignment_history
         WHERE at < now() - make_interval(days => :days)
    """), {"days": HISTORY_KEEP_DAYS})
    return result.rowcount or 0


async def shard_rows(session: AsyncSession) -> list[dict[str, Any]]:
    """Display columns for every ownable endpoint, with its recorded
    assignment (docs/26 Phase 5's shard map). Same predicate as
    ownable_endpoints; the owner itself is not decided here - the service
    computes it from the plan, the same way build_assignment does."""
    rows = (await session.execute(text("""
        SELECT e.id::text AS id, e.device_id::text AS device_id,
               d.name AS device_name, d.device_type,
               e.protocol::text AS protocol, host(e.address) AS address,
               e.collector_id AS pinned_to,
               ea.collector_id AS recorded_owner, ea.epoch, ea.since, ea.reason
          FROM device_endpoint e
          JOIN device d        ON d.id = e.device_id
          JOIN poll_profile p  ON p.id = e.poll_profile_id
          LEFT JOIN endpoint_assignment ea ON ea.endpoint_id = e.id
         WHERE e.enabled AND e.admin_state = 'enabled'
           AND d.lifecycle <> 'decommissioned'
    """))).mappings().all()
    return [dict(r) for r in rows]


async def assignment_history(session: AsyncSession, endpoint_id: str,
                             limit: int = 50) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT from_collector, to_collector, epoch, reason, at
          FROM endpoint_assignment_history
         WHERE endpoint_id = CAST(:id AS uuid)
         ORDER BY at DESC, id DESC
         LIMIT :limit
    """), {"id": endpoint_id, "limit": limit})).mappings().all()
    return [dict(r) for r in rows]


async def recent_moves(session: AsyncSession, hours: int = 24) -> dict[str, int]:
    """Moves in the last `hours`, by reason - seeded rows included only if
    they fall inside the window, which after the first day they will not."""
    rows = (await session.execute(text("""
        SELECT reason, count(*) AS n FROM endpoint_assignment_history
         WHERE at > now() - make_interval(hours => :h)
         GROUP BY reason
    """), {"h": hours})).mappings().all()
    return {r["reason"]: int(r["n"]) for r in rows}


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
               c.kind AS credential_kind, c.secret_enc, c.key_id AS credential_key_id
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


#: When a heartbeat starts a NEW healthy streak (docs/26 Phase 6's failback
#: damping measures its ten minutes from healthy_since). One fragment for both
#: heartbeat paths - the Redis stream (ingest worker) and the HTTP fallback -
#: and the live test, so the three cannot drift: the test used to carry its
#: own copy of the worker's SQL. A streak starts on the first heartbeat ever
#: (no last_heartbeat, or a record created ahead of its install that has never
#: run), after a gap past the 60 s stale cutoff, or when none is recorded. The
#: last two were missing: a pre-created record carries a creation-time
#: last_heartbeat, so its first real heartbeat fell inside the window and the
#: streak stayed NULL for good - both second BMS members ran half an hour in
#: the live HA test with none. clock_timestamp(), not now(): one transaction
#: can hold several heartbeats for the same collector.
HEALTHY_SINCE_ON_HEARTBEAT = """CASE
                WHEN collector_instance.last_heartbeat IS NULL
                     OR collector_instance.healthy_since IS NULL
                     OR collector_instance.started_at IS NULL
                     OR clock_timestamp() - collector_instance.last_heartbeat
                        > interval '60 seconds'
                  THEN clock_timestamp()
                ELSE collector_instance.healthy_since
            END"""


#: When a heartbeat proves a SECOND live process under this collector's
#: identity. One process's started_at only ever moves forward - a restart,
#: an upgrade's re-exec - so a heartbeat whose started_at is OLDER than the
#: stored one came from another process that is still running. Found live: a
#: hand-started test collector outlived a stack restart, two processes ran as
#: col-dc1-bms for a quarter of an hour, double-polled 95 endpoints, fought
#: over its trap and health ports, and nothing anywhere said so.
DUPLICATE_SEEN_ON_HEARTBEAT = """CASE
                WHEN collector_instance.started_at IS NOT NULL
                     AND EXCLUDED.started_at IS NOT NULL
                     AND EXCLUDED.started_at < collector_instance.started_at - interval '1 second'
                  THEN to_jsonb(clock_timestamp())
                ELSE COALESCE(collector_instance.stats->'duplicate_seen_at', 'null'::jsonb)
            END"""


async def upsert_heartbeat(session: AsyncSession, hb: dict[str, Any]) -> None:
    """The HTTP-fallback heartbeat (a gateway-transport collector). Keeps the
    same three facts the Redis path keeps, which it did not: started_at (so a
    collector created ahead of its install ever counts as having run - without
    it `accepting` stayed False and it was never given work), and the
    healthy_since streak docs/26 Phase 6's failover and failback read."""
    await session.execute(text("""
        INSERT INTO collector_instance (id, version, hostname, started_at,
                                        last_heartbeat, endpoints_owned,
                                        endpoints_online, status, stats, state,
                                        healthy_since)
        VALUES (:id, :version, :hostname, :started_at, clock_timestamp(),
                :endpoints_owned, :endpoints_online, 'HEALTHY', CAST(:stats AS jsonb),
                CASE WHEN EXISTS (SELECT 1 FROM collector_instance
                                   WHERE state <> 'decommissioned')
                     THEN 'pending' ELSE 'active' END,
                clock_timestamp())
        ON CONFLICT (id) DO UPDATE SET
            version = EXCLUDED.version,
            hostname = EXCLUDED.hostname,
            started_at = COALESCE(EXCLUDED.started_at, collector_instance.started_at),
            healthy_since = __HEALTHY_SINCE__,
            last_heartbeat = clock_timestamp(),
            endpoints_owned = EXCLUDED.endpoints_owned,
            endpoints_online = EXCLUDED.endpoints_online,
            status = 'HEALTHY',
            stats = EXCLUDED.stats || jsonb_build_object(
                'duplicate_seen_at', __DUPLICATE_SEEN__)
    """.replace("__HEALTHY_SINCE__", HEALTHY_SINCE_ON_HEARTBEAT)
       .replace("__DUPLICATE_SEEN__", DUPLICATE_SEEN_ON_HEARTBEAT)), hb)
