"""collector_pool - site x plane placement (docs/26 Phase 5, migration 0088).

The table has existed since 0088 and the assigner has read it since then;
what was missing until this file was any way to WRITE it short of raw SQL,
so every deployment had zero rows and every pool-level column (trap_vip,
bbmd_settings, rate_budget_points_per_s) was dead on arrival. This is the
operator-facing side the sharding path always assumed would exist.

Raw SQL like the rest of repositories/ - there is deliberately no ORM model
for this table, the same choice repositories/collector.py made for
collector_instance's placement columns.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Resolves an endpoint to its pool the same way assignment_endpoints and
# ownable_endpoints do: an explicit device_endpoint.pool_id wins, otherwise
# the most specific enabled discovery_range containing the endpoint's own
# address, joined to the pool sharing that range's datacenter and purpose.
# Kept as one fragment so the three call sites cannot drift apart.
_RESOLVED_POOL = """
    COALESCE(e.pool_id, (
        SELECT cp.id
          FROM discovery_range dr
          JOIN collector_pool cp
            ON cp.datacenter_id = dc.id AND cp.plane = dr.purpose
         WHERE e.address IS NOT NULL AND e.address <<= dr.cidr
         ORDER BY masklen(dr.cidr) DESC
         LIMIT 1
    ))
"""

_SELECT = """
    SELECT cp.id::text, cp.name, cp.datacenter_id::text AS datacenter_id,
           dc.code AS site, dc.name AS site_name, cp.plane,
           cp.cidrs::text[] AS cidrs, host(cp.trap_vip) AS trap_vip,
           cp.bbmd_settings, cp.rate_budget_points_per_s, cp.min_members,
           cp.created_at, cp.updated_at
      FROM collector_pool cp
      JOIN datacenter dc ON dc.id = cp.datacenter_id
"""


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["cidrs"] = list(d.get("cidrs") or [])
    d["bbmd_settings"] = dict(d.get("bbmd_settings") or {})
    return d


async def list_pools(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text(_SELECT + " ORDER BY dc.code, cp.plane"))
            ).mappings().all()
    return [_row(r) for r in rows]


async def get_pool(session: AsyncSession, pool_id: str) -> dict[str, Any] | None:
    row = (await session.execute(text(_SELECT + " WHERE cp.id = CAST(:id AS uuid)"),
                                 {"id": pool_id})).mappings().first()
    return _row(row) if row else None


async def pools_by_ids(session: AsyncSession, ids: list[str]) -> list[dict[str, Any]]:
    """The pools an assignment has to carry - see services/collector.
    build_assignment. Empty in, empty out, no query."""
    if not ids:
        return []
    rows = (await session.execute(
        text(_SELECT + " WHERE cp.id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": ids})).mappings().all()
    return [_row(r) for r in rows]


async def create_pool(session: AsyncSession, values: dict[str, Any]) -> str:
    """INSERT and return the new id. The (datacenter_id, plane) unique
    constraint is left to raise: the service turns it into a 409, and
    checking first would only add a race it cannot close."""
    return await session.scalar(text("""
        INSERT INTO collector_pool (name, datacenter_id, plane, cidrs, trap_vip,
                                    bbmd_settings, rate_budget_points_per_s,
                                    min_members)
        VALUES (:name, CAST(:datacenter_id AS uuid), :plane,
                CAST(:cidrs AS text[])::cidr[], CAST(:trap_vip AS inet),
                CAST(:bbmd_settings AS jsonb), :rate_budget_points_per_s,
                :min_members)
        RETURNING id::text
    """), {
        "name": values["name"],
        "datacenter_id": values["datacenter_id"],
        "plane": values["plane"],
        "cidrs": list(values.get("cidrs") or []),
        "trap_vip": values.get("trap_vip"),
        "bbmd_settings": json.dumps(values.get("bbmd_settings") or {}),
        "rate_budget_points_per_s": values.get("rate_budget_points_per_s"),
        "min_members": int(values.get("min_members") or 1),
    })


#: Columns an edit may touch, and how each is written - the same shape
#: repositories/devices._EDITABLE uses, for the same reason: a typo in a
#: caller's key is a KeyError here, never a silently ignored setting.
_EDITABLE = {
    "name":                     "name = :name",
    "cidrs":                    "cidrs = CAST(:cidrs AS text[])::cidr[]",
    "trap_vip":                 "trap_vip = CAST(:trap_vip AS inet)",
    "bbmd_settings":            "bbmd_settings = CAST(:bbmd_settings AS jsonb)",
    "rate_budget_points_per_s": "rate_budget_points_per_s = :rate_budget_points_per_s",
    "min_members":              "min_members = :min_members",
}


async def update_pool(session: AsyncSession, pool_id: str,
                      changes: dict[str, Any]) -> None:
    if not changes:
        return
    sets = ", ".join(_EDITABLE[k] for k in changes)
    params: dict[str, Any] = {"id": pool_id, **changes}
    if "cidrs" in params:
        params["cidrs"] = list(params["cidrs"] or [])
    if "bbmd_settings" in params:
        params["bbmd_settings"] = json.dumps(params["bbmd_settings"] or {})
    await session.execute(text(f"""
        UPDATE collector_pool SET {sets}, updated_at = now()
         WHERE id = CAST(:id AS uuid)
    """), params)


async def delete_pool(session: AsyncSession, pool_id: str) -> bool:
    result = await session.execute(text("""
        DELETE FROM collector_pool WHERE id = CAST(:id AS uuid)
    """), {"id": pool_id})
    return bool(result.rowcount)


async def members(session: AsyncSession,
                  stale_after_s: float = 60.0) -> list[dict[str, Any]]:
    """Every collector placed in any pool, decommissioned ones excluded.

    Pending collectors ARE included - the one thing an operator opens a
    pool page to find out is "why is my new collector not polling", and
    the answer "it is pending approval" has to be visible there.
    `healthy`/`accepting` use the same rules as repositories/collector.
    live_collectors so this page and the assigner never disagree.
    """
    rows = (await session.execute(text("""
        SELECT ci.id, ci.pool_id::text AS pool_id, ci.state, ci.status,
               ci.hostname, ci.version,
               ci.started_at IS NOT NULL AS has_run,
               extract(epoch FROM (clock_timestamp() - ci.last_heartbeat)) AS age_s,
               ci.endpoints_owned, ci.endpoints_online,
               (ci.stats->'capacity'->>'busy_pct')::float AS busy_pct
          FROM collector_instance ci
         WHERE ci.pool_id IS NOT NULL AND ci.state <> 'decommissioned'
         ORDER BY ci.id
    """), {})).mappings().all()
    out = []
    for r in rows:
        age = float(r["age_s"]) if r["age_s"] is not None else None
        out.append({
            "collector_id": r["id"], "pool_id": r["pool_id"],
            "state": r["state"], "status": r["status"],
            "hostname": r["hostname"], "version": r["version"],
            "healthy": age is not None and age < stale_after_s,
            "accepting": r["state"] == "active" and bool(r["has_run"]),
            # A record created ahead of its install carries a creation-time
            # heartbeat, so `healthy` alone cannot tell "fine" from "never ran".
            "has_run": bool(r["has_run"]),
            "heartbeat_age_s": age,
            "endpoints_owned": r["endpoints_owned"],
            "endpoints_online": r["endpoints_online"],
            # docs/26 Phase 5: poll-worker busy % over the trailing window,
            # from the last heartbeat. Only while that heartbeat is fresh -
            # a silent collector's last figure is history, not load.
            "busy_pct": (float(r["busy_pct"]) if r["busy_pct"] is not None
                         and age is not None and age < stale_after_s else None),
        })
    return out


async def pool_points(session: AsyncSession,
                      stale_after_s: float = 60.0) -> dict[str, float]:
    """Points/s measured from each pool's endpoints (docs/26 Phase 5),
    summed over every live collector reporting them - member or not, since a
    pinned endpoint can be polled from outside its pool. A pool no live
    collector reports on is absent, not zero."""
    rows = (await session.execute(text("""
        SELECT kv.key AS pool_id, sum((kv.value->>'points_per_s')::float) AS pts
          FROM collector_instance ci,
               jsonb_each(ci.stats->'capacity'->'pools') AS kv
         WHERE ci.state NOT IN ('decommissioned', 'pending')
           AND jsonb_typeof(ci.stats->'capacity'->'pools') = 'object'
           AND clock_timestamp() - ci.last_heartbeat < make_interval(secs => :stale)
         GROUP BY kv.key
    """), {"stale": stale_after_s})).mappings().all()
    return {r["pool_id"]: float(r["pts"]) for r in rows if r["pts"] is not None}


async def collector_count(session: AsyncSession, pool_id: str) -> int:
    return int(await session.scalar(text("""
        SELECT count(*) FROM collector_instance
         WHERE pool_id = CAST(:id AS uuid) AND state <> 'decommissioned'
    """), {"id": pool_id}) or 0)


async def endpoint_counts(session: AsyncSession) -> list[dict[str, Any]]:
    """Ownable endpoints per (resolved pool, protocol).

    Resolved the same way the assigner resolves them, so the count a pool
    page shows is the count the assigner will actually shard - an endpoint
    listed here with no pool is exactly one nothing pool-placed can own.
    """
    rows = (await session.execute(text(f"""
        SELECT ({_RESOLVED_POOL})::text AS pool_id,
               e.protocol::text AS protocol, count(*) AS n
          FROM device_endpoint e
          JOIN device d        ON d.id = e.device_id
          LEFT JOIN rack rk    ON rk.id = d.rack_id
          LEFT JOIN rack_row rr ON rr.id = rk.row_id
          LEFT JOIN room rm    ON rm.id = COALESCE(rr.room_id, d.room_id)
          LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
         WHERE e.enabled AND e.admin_state = 'enabled'
           AND d.lifecycle <> 'decommissioned'
         GROUP BY 1, 2
    """))).mappings().all()
    return [dict(r) for r in rows]


async def probe_rows(session: AsyncSession, *, endpoint_ids: list[str] | None = None,
                     pool_id: str | None = None) -> list[dict[str, Any]]:
    """Address, port and protocol for reachability probing (docs/26 Phase 8)
    - either the named endpoints, or every endpoint resolving into a pool.
    Credential-free by construction: preflight runs before a collector has
    been trusted with anything, and needs only where to knock."""
    if endpoint_ids is not None:
        if not endpoint_ids:
            return []
        where, params = "e.id = ANY(CAST(:ids AS uuid[]))", {"ids": endpoint_ids}
    elif pool_id is not None:
        where, params = f"({_RESOLVED_POOL}) = CAST(:pool AS uuid)", {"pool": pool_id}
    else:
        return []
    rows = (await session.execute(text(f"""
        SELECT e.id::text, e.protocol::text AS protocol,
               host(e.address) AS address, e.port
          FROM device_endpoint e
          JOIN device d        ON d.id = e.device_id
          LEFT JOIN rack rk    ON rk.id = d.rack_id
          LEFT JOIN rack_row rr ON rr.id = rk.row_id
          LEFT JOIN room rm    ON rm.id = COALESCE(rr.room_id, d.room_id)
          LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
         WHERE e.enabled AND e.admin_state = 'enabled'
           AND d.lifecycle <> 'decommissioned' AND e.address IS NOT NULL
           AND {where}
    """), params)).mappings().all()
    return [dict(r) for r in rows]


async def ranges_for(session: AsyncSession, pool_id: str) -> list[dict[str, Any]]:
    """The discovery ranges whose endpoints resolve into this pool: same
    datacenter, purpose == plane. What the firewall matrix's destination
    column is built from."""
    rows = (await session.execute(text("""
        SELECT dr.id::text, dr.cidr::text AS cidr, dr.name, dr.enabled,
               dr.exclusions::text[] AS exclusions
          FROM discovery_range dr
          JOIN collector_pool cp
            ON cp.datacenter_id = dr.datacenter_id AND cp.plane = dr.purpose
         WHERE cp.id = CAST(:id AS uuid)
         ORDER BY dr.cidr
    """), {"id": pool_id})).mappings().all()
    return [{**dict(r), "exclusions": list(r["exclusions"] or [])} for r in rows]


async def readiness_counts(session: AsyncSession, pool_id: str) -> list[dict[str, Any]]:
    """Per protocol, for the endpoints resolving into one pool: how many,
    how many carry a credential, and how many the last poll left in each
    comm state (docs/26 Phase 8's onboarding wizard, credentials and
    verification steps). Resolved with the same fragment as everything
    else here, so the wizard counts exactly what the assigner shards."""
    rows = (await session.execute(text(f"""
        SELECT e.protocol::text AS protocol,
               count(*) AS endpoints,
               count(e.credential_id) AS with_credential,
               count(*) FILTER (WHERE es.status::text = 'ONLINE')   AS online,
               count(*) FILTER (WHERE es.status::text = 'DEGRADED') AS degraded,
               count(*) FILTER (WHERE es.status::text = 'OFFLINE')  AS offline,
               count(*) FILTER (WHERE es.status IS NULL
                                   OR es.status::text = 'UNKNOWN') AS never_polled
          FROM device_endpoint e
          JOIN device d        ON d.id = e.device_id
          LEFT JOIN rack rk    ON rk.id = d.rack_id
          LEFT JOIN rack_row rr ON rr.id = rk.row_id
          LEFT JOIN room rm    ON rm.id = COALESCE(rr.room_id, d.room_id)
          LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
          LEFT JOIN endpoint_state es ON es.endpoint_id = e.id
         WHERE e.enabled AND e.admin_state = 'enabled'
           AND d.lifecycle <> 'decommissioned'
           AND ({_RESOLVED_POOL}) = CAST(:pool AS uuid)
         GROUP BY 1
         ORDER BY 1
    """), {"pool": pool_id})).mappings().all()
    return [{k: (int(v) if k != "protocol" else v) for k, v in r.items()} for r in rows]
