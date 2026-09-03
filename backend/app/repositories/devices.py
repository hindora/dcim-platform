"""Device, endpoint and interface queries. All SQL for this domain lives here."""

from __future__ import annotations

import base64
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Devices are listed newest-first by name; the cursor is (name, id) so that
# concurrent inserts cannot make a page repeat or skip rows the way OFFSET does.
_LIST_BASE = """
    SELECT d.id::text, d.name, d.device_type,
           v.name AS vendor, m.name AS model,
           host(d.mgmt_ip)    AS mgmt_ip,
           host(d.primary_ip) AS primary_ip,
           COALESCE(ds.status::text, 'UNKNOWN')       AS status,
           COALESCE(ds.health::text, 'UNKNOWN')       AS health,
           COALESCE(ds.max_severity::text, 'CLEAR')   AS max_severity,
           ds.last_seen,
           dc.id::text AS datacenter_id, dc.code AS datacenter_code,
           rm.id::text AS room_id, rm.name AS room_name,
           rr.name AS row_name,
           r.id::text AS rack_id, r.name AS rack_name,
           d.u_start,
           -- Asset-view columns. Additive: every consumer that predates the
           -- asset module ignores them, which is what keeps the pages outside
           -- /assets unchanged (docs/22 §1).
           d.serial_number, d.asset_tag,
           d.lifecycle::text AS lifecycle,
           d.warranty_expires, d.owner_group, d.cost_centre,
           -- Derived server-side, from ONE threshold, so the tile, the filter
           -- and the asset record cannot disagree about what "expiring" means.
           CASE WHEN d.warranty_expires IS NULL THEN 'unknown'
                WHEN d.warranty_expires < CURRENT_DATE THEN 'expired'
                WHEN d.warranty_expires <= CURRENT_DATE + 90 THEN 'expiring'
                ELSE 'active' END AS warranty_state,
           dt.category
    FROM device d
    LEFT JOIN vendor v        ON v.id = d.vendor_id
    LEFT JOIN model m         ON m.id = d.model_id
    LEFT JOIN device_type dt  ON dt.code = d.device_type
    LEFT JOIN device_state ds ON ds.device_id = d.id
    LEFT JOIN rack r          ON r.id = d.rack_id
    LEFT JOIN rack_row rr     ON rr.id = r.row_id
    LEFT JOIN room rm         ON rm.id = COALESCE(rr.room_id, d.room_id)
    LEFT JOIN datacenter dc   ON dc.id = rm.datacenter_id
"""


def encode_cursor(name: str, device_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([name, device_id]).encode()).decode()


def decode_cursor(cursor: str) -> tuple[str, str] | None:
    try:
        name, device_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return name, device_id
    except Exception:
        return None


def _filters(
    *,
    device_types: list[str] | None = None,
    status: list[str] | None = None,
    room_id: str | None = None,
    rack_id: str | None = None,
    datacenter_id: str | None = None,
    search: str | None = None,
    include_decommissioned: bool = False,
    lifecycle: list[str] | None = None,
    category: list[str] | None = None,
    vendor_id: str | None = None,
    asset_tag: str | None = None,
    serial_number: str | None = None,
    has_serial: bool | None = None,
    warranty_state: str | None = None,
    warranty_before: str | None = None,
    supplier_id: str | None = None,
    owner_group: str | None = None,
    cost_centre: str | None = None,
    tags: list[str] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Every predicate the list understands, shared with the count.

    One builder rather than two, because a count assembled separately
    drifts from the query it is supposed to describe - and a total that
    disagrees with the rows underneath it is worse than no total, since
    the last page then looks broken.

    The CURSOR is deliberately not here: it narrows a page, not the result
    set, and folding it in would make the total shrink as somebody paged.
    """
    where: list[str] = []
    params: dict[str, Any] = {}

    # An explicit lifecycle filter replaces the default hiding of
    # decommissioned rows rather than stacking with it - otherwise asking for
    # decommissioned devices returns nothing, which reads as "there are none".
    if lifecycle:
        where.append("d.lifecycle::text = ANY(:lifecycle)")
        params["lifecycle"] = lifecycle
    elif not include_decommissioned:
        where.append("d.lifecycle <> 'decommissioned'")
    if device_types:
        where.append("d.device_type = ANY(:device_types)")
        params["device_types"] = device_types
    if status:
        where.append("COALESCE(ds.status::text, 'UNKNOWN') = ANY(:status)")
        params["status"] = status
    if room_id:
        where.append("rm.id = CAST(:room_id AS uuid)")
        params["room_id"] = room_id
    if rack_id:
        where.append("d.rack_id = CAST(:rack_id AS uuid)")
        params["rack_id"] = rack_id
    if datacenter_id:
        where.append("dc.id = CAST(:datacenter_id AS uuid)")
        params["datacenter_id"] = datacenter_id
    if category:
        where.append("dt.category = ANY(:category)")
        params["category"] = category
    if vendor_id:
        where.append("d.vendor_id = CAST(:vendor_id AS uuid)")
        params["vendor_id"] = vendor_id
    if asset_tag:
        where.append("d.asset_tag = :asset_tag")
        params["asset_tag"] = asset_tag
    if serial_number:
        where.append("d.serial_number = :serial_number")
        params["serial_number"] = serial_number
    if has_serial is not None:
        # `has_serial=false` is the reconciliation work queue, and today it
        # returns the whole estate (docs/19 B2). That is the point of exposing
        # it as a filter rather than only as a number on a tile.
        where.append("d.serial_number IS NOT NULL" if has_serial
                     else "d.serial_number IS NULL")
    if warranty_state == "unknown":
        where.append("d.warranty_expires IS NULL")
    elif warranty_state == "expired":
        where.append("d.warranty_expires < CURRENT_DATE")
    elif warranty_state == "expiring":
        where.append("d.warranty_expires >= CURRENT_DATE "
                     "AND d.warranty_expires <= CURRENT_DATE + 90")
    elif warranty_state == "active":
        where.append("d.warranty_expires > CURRENT_DATE + 90")
    if warranty_before:
        where.append("d.warranty_expires < CAST(:warranty_before AS date)")
        params["warranty_before"] = warranty_before
    if supplier_id:
        where.append("d.supplier_id = CAST(:supplier_id AS uuid)")
        params["supplier_id"] = supplier_id
    if owner_group:
        where.append("d.owner_group = :owner_group")
        params["owner_group"] = owner_group
    if cost_centre:
        where.append("d.cost_centre = :cost_centre")
        params["cost_centre"] = cost_centre
    if tags:
        # AND within the filter: `tag=env:prod&tag=tier:1` means devices that
        # carry BOTH, which is what somebody narrowing a list expects. An OR
        # would widen the result as they added filters.
        for i, spec in enumerate(tags):
            key, _, value = spec.partition(":")
            where.append(
                f"EXISTS (SELECT 1 FROM tag_assignment ta JOIN tag tg "
                f"ON tg.id = ta.tag_id WHERE ta.object_type = 'device' "
                f"AND ta.object_id = d.id AND tg.key = :tag_k{i} "
                f"AND tg.value = :tag_v{i})")
            params[f"tag_k{i}"] = key
            params[f"tag_v{i}"] = value
    if search:
        # Trigram index on name; the IP casts are cheap because the result set
        # is already narrowed by the other predicates in practice.
        where.append("(d.name ILIKE :search OR host(d.mgmt_ip) LIKE :like "
                     "OR host(d.primary_ip) LIKE :like OR d.serial_number ILIKE :search "
                     "OR d.asset_tag ILIKE :search)")
        params["search"] = f"%{search}%"
        params["like"] = f"%{search}%"
    return where, params


async def list_devices(
    session: AsyncSession,
    *,
    device_types: list[str] | None = None,
    status: list[str] | None = None,
    room_id: str | None = None,
    rack_id: str | None = None,
    datacenter_id: str | None = None,
    search: str | None = None,
    include_decommissioned: bool = False,
    lifecycle: list[str] | None = None,
    category: list[str] | None = None,
    vendor_id: str | None = None,
    asset_tag: str | None = None,
    serial_number: str | None = None,
    has_serial: bool | None = None,
    warranty_state: str | None = None,
    warranty_before: str | None = None,
    supplier_id: str | None = None,
    owner_group: str | None = None,
    cost_centre: str | None = None,
    tags: list[str] | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    where, params = _filters(
        device_types=device_types, status=status, room_id=room_id,
        rack_id=rack_id, datacenter_id=datacenter_id, search=search,
        include_decommissioned=include_decommissioned, lifecycle=lifecycle,
        category=category, vendor_id=vendor_id, asset_tag=asset_tag,
        serial_number=serial_number, has_serial=has_serial,
        warranty_state=warranty_state, warranty_before=warranty_before,
        supplier_id=supplier_id, owner_group=owner_group,
        cost_centre=cost_centre, tags=tags)
    params["limit"] = limit + 1

    if cursor:
        decoded = decode_cursor(cursor)
        if decoded:
            where.append("(d.name, d.id::text) > (:cur_name, :cur_id)")
            params["cur_name"], params["cur_id"] = decoded

    sql = _LIST_BASE
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY d.name, d.id::text LIMIT :limit"

    rows = (await session.execute(text(sql), params)).mappings().all()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1]["name"], rows[-1]["id"])
    return [dict(r) for r in rows], next_cursor


async def count_matching(session: AsyncSession, **filters: Any) -> int:
    """How many rows the filters select, ignoring paging.

    Uses the same joins as the list because the predicates reach into them -
    device_state for status, device_type for category, the rack chain for room
    and site. A leaner count would be a different query answering a different
    question.
    """
    where, params = _filters(**filters)
    sql = """
        SELECT count(*)
        FROM device d
        LEFT JOIN vendor v        ON v.id = d.vendor_id
        LEFT JOIN model m         ON m.id = d.model_id
        LEFT JOIN device_type dt  ON dt.code = d.device_type
        LEFT JOIN device_state ds ON ds.device_id = d.id
        LEFT JOIN rack r          ON r.id = d.rack_id
        LEFT JOIN rack_row rr     ON rr.id = r.row_id
        LEFT JOIN room rm         ON rm.id = COALESCE(rr.room_id, d.room_id)
        LEFT JOIN datacenter dc   ON dc.id = rm.datacenter_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    return (await session.execute(text(sql), params)).scalar_one()


async def count_devices(session: AsyncSession) -> int:
    return (await session.execute(text(
        "SELECT count(*) FROM device WHERE lifecycle <> 'decommissioned'"
    ))).scalar_one()


async def get_device(session: AsyncSession, device_id: str) -> dict[str, Any] | None:
    # Detail needs extra columns, and they must land in the SELECT list rather
    # than after it, so the shared base is patched rather than concatenated.
    sql = _LIST_BASE.replace(
        "           dt.category\n",
        "           dt.category, d.u_height, d.facing,\n"
        "           d.admin_state::text AS admin_state,\n"
        "           d.attributes,\n"
        # The nameplate. rated_power_w is the MODEL's, not the device's:
        # every R640 draws from the same datasheet, and a per-device override
        # would be a measurement pretending to be a rating.
        "           m.rated_power_w, m.u_height AS model_u_height\n",
    ) + " WHERE d.id = CAST(:id AS uuid)"
    row = (await session.execute(text(sql), {"id": device_id})).mappings().first()
    return dict(row) if row else None


async def list_power_supplies(session: AsyncSession,
                              device_id: str) -> list[dict[str, Any]]:
    """The PSUs a device is built with, in slot order.

    Inventory, not telemetry: what the chassis has and what each inlet is
    rated for. Two 1100 W C14 supplies is a different machine from one, and it
    is the fact behind whether losing a feed costs you the server - which is
    what an operator is asking when they open this page.
    """
    # ...and what feeds each one. A cord lands on an outlet at the far end, so
    # the PSU is the B side of a power connection and the PDU is the A side -
    # the mirror of the outlet query, which walks the same cable the other way.
    #
    # This is the fact that decides whether two supplies are redundancy or
    # decoration: two cords into one strip is a single point of failure wearing
    # a pair of PSUs.
    rows = (await session.execute(text("""
        SELECT ps.number, ps.connector, ps.rated_watts,
               fd.id::text AS feed_device_id,
               fd.name     AS feed_device,
               o.number    AS feed_outlet
          FROM power_supply ps
          LEFT JOIN connection c ON c.b_termination_type = 'psu'
                                AND c.b_termination_id = ps.id
          LEFT JOIN device fd ON fd.id = c.a_device_id
          LEFT JOIN outlet o  ON o.id = c.a_termination_id
         WHERE ps.device_id = CAST(:id AS uuid)
         ORDER BY ps.number
    """), {"id": device_id})).mappings().all()
    return [dict(r) for r in rows]


async def get_device_state(session: AsyncSession, device_id: str) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        SELECT device_id::text, status::text AS status, health::text AS health,
               max_severity::text AS max_severity, active_alarms, last_seen, metrics
        FROM device_state WHERE device_id = CAST(:id AS uuid)
    """), {"id": device_id})).mappings().first()
    return dict(row) if row else None


async def list_endpoints(session: AsyncSession, device_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT e.id::text, e.protocol::text AS protocol, e.role::text AS role,
               host(e.address) AS address, e.port, e.enabled,
               e.admin_state::text AS admin_state,
               e.addressing,
               e.credential_id::text AS credential_id,
               c.name AS credential_name,
               c.secret_hint AS credential_hint,
               e.poll_profile_id::text AS poll_profile_id,
               p.name AS poll_profile_name,
               e.via_endpoint_id::text AS via_endpoint_id,
               -- The gateway's own identity, so the UI can say WHICH gateway
               -- an endpoint sits behind rather than only that one exists.
               vd.name AS via_name,
               p.interval_s AS poll_interval_s,
               COALESCE(es.status::text, 'UNKNOWN') AS status,
               es.last_seen, es.last_success, es.last_error, es.last_error_class,
               COALESCE(es.consecutive_failures, 0) AS consecutive_failures,
               es.last_latency_ms,
               -- Cumulative since the row was first written. The collector
               -- resets its own counters on restart and the writer keeps the
               -- stored value monotonic, so these are lifetime totals, not a
               -- recent window - the UI has to say so.
               COALESCE(es.poll_count, 0) AS poll_count,
               COALESCE(es.fail_count, 0) AS fail_count,
               COALESCE(es.timeout_count, 0) AS timeout_count,
               COALESCE(es.auth_fail_count, 0) AS auth_fail_count
        FROM device_endpoint e
        LEFT JOIN credential c     ON c.id = e.credential_id
        JOIN poll_profile p        ON p.id = e.poll_profile_id
        LEFT JOIN endpoint_state es ON es.endpoint_id = e.id
        LEFT JOIN device_endpoint ve ON ve.id = e.via_endpoint_id
        LEFT JOIN device vd          ON vd.id = ve.device_id
        WHERE e.device_id = CAST(:id AS uuid)
        ORDER BY e.protocol, e.role
    """), {"id": device_id})).mappings().all()
    return [dict(r) for r in rows]


async def get_endpoint(session: AsyncSession, endpoint_id: str
                       ) -> dict[str, Any] | None:
    """One endpoint with everything an edit has to be checked against."""
    row = (await session.execute(text("""
        SELECT e.id::text, e.device_id::text AS device_id,
               e.protocol::text AS protocol, e.role::text AS role,
               host(e.address) AS address, e.port, e.enabled,
               e.admin_state::text AS admin_state, e.addressing,
               e.credential_id::text AS credential_id,
               e.poll_profile_id::text AS poll_profile_id,
               e.via_endpoint_id::text AS via_endpoint_id,
               vd.name AS via_name, d.name AS device_name
          FROM device_endpoint e
          JOIN device d ON d.id = e.device_id
          LEFT JOIN device_endpoint ve ON ve.id = e.via_endpoint_id
          LEFT JOIN device vd          ON vd.id = ve.device_id
         WHERE e.id = CAST(:id AS uuid)
    """), {"id": endpoint_id})).mappings().first()
    return dict(row) if row else None


#: Columns an edit may touch, and how each is written.
#:
#: A whitelist rather than string-built SQL: this statement takes operator
#: input and writes the row a collector will act on, and a column name is not
#: the kind of thing that should ever arrive from a browser.
_EDITABLE = {
    "address":         "address = CAST(:address AS inet)",
    "port":            "port = :port",
    "addressing":      "addressing = CAST(:addressing AS jsonb)",
    "credential_id":   "credential_id = CAST(:credential_id AS uuid)",
    "poll_profile_id": "poll_profile_id = CAST(:poll_profile_id AS uuid)",
    "enabled":         "enabled = :enabled",
    "admin_state":     "admin_state = CAST(:admin_state AS admin_state_t)",
}


async def update_endpoint(session: AsyncSession, endpoint_id: str,
                          changes: dict[str, Any]) -> dict[str, Any] | None:
    """Apply an edit and bump updated_at.

    updated_at is what the assignment `version` is derived from, so touching it
    is not bookkeeping - it is the entire delivery mechanism. Every collector
    holding this endpoint sees a new version on its next assignment fetch, and
    the change takes effect within one assignment interval without anything
    being restarted.
    """
    if not changes:
        return await get_endpoint(session, endpoint_id)
    sets = ", ".join(_EDITABLE[k] for k in changes)
    params: dict[str, Any] = {"id": endpoint_id, **changes}
    if "addressing" in params:
        params["addressing"] = json.dumps(params["addressing"])
    await session.execute(text(f"""
        UPDATE device_endpoint
           SET {sets}, updated_at = now()
         WHERE id = CAST(:id AS uuid)
    """), params)
    return await get_endpoint(session, endpoint_id)


async def list_credentials(session: AsyncSession, *, protocol: str | None = None,
                           q: str | None = None, limit: int = 50,
                           include_id: str | None = None) -> list[dict[str, Any]]:
    """Named credentials for the picker. Hints only - never a secret.

    Filtered and capped, because "how many credentials can there be" has a bad
    answer on a real estate: this fleet holds 894 SNMP credentials, one per
    device, since the community string is per-device. An unfiltered picker is
    a 45 KB payload and a dropdown nobody can use.

    ``include_id`` is always returned regardless of the filter - the endpoint's
    CURRENT credential must appear in its own editor even when it falls outside
    the first page of matches.
    """
    rows = (await session.execute(text("""
        WITH matched AS (
            SELECT c.id, c.name, c.protocol::text AS protocol, c.kind,
                   c.secret_hint, c.rotated_at,
                   (c.id = CAST(:include_id AS uuid)) AS current
              FROM credential c
             WHERE (CAST(:protocol AS text) IS NULL
                OR c.protocol::text = CAST(:protocol AS text))
               AND (CAST(:q AS text) IS NULL
                OR c.name ILIKE '%' || CAST(:q AS text) || '%')
             ORDER BY current DESC NULLS LAST, c.name
             LIMIT CAST(:limit AS integer)
        ), plus_current AS (
            SELECT * FROM matched
            UNION
            SELECT c.id, c.name, c.protocol::text, c.kind, c.secret_hint,
                   c.rotated_at, TRUE
              FROM credential c
             WHERE c.id = CAST(:include_id AS uuid)
        )
        SELECT p.id::text, p.name, p.protocol, p.kind, p.secret_hint,
               p.rotated_at, p.current,
               count(e.id) AS endpoints
          FROM plus_current p
          LEFT JOIN device_endpoint e ON e.credential_id = p.id
         GROUP BY p.id, p.name, p.protocol, p.kind, p.secret_hint,
                  p.rotated_at, p.current
         ORDER BY p.current DESC, p.name
    """), {"protocol": protocol, "q": q, "limit": limit,
           "include_id": include_id})).mappings().all()
    return [dict(r) for r in rows]


async def count_credentials(session: AsyncSession, protocol: str | None = None
                            ) -> int:
    """How many exist behind the capped list, so the UI can say so."""
    return int((await session.execute(text("""
        SELECT count(*) FROM credential
         WHERE (CAST(:protocol AS text) IS NULL
            OR protocol::text = CAST(:protocol AS text))
    """), {"protocol": protocol})).scalar_one())


async def list_poll_profiles(session: AsyncSession) -> list[dict[str, Any]]:
    """Every profile with what it is currently steering.

    The counts are the point. A profile is shared - `redfish-60s` carries 310
    endpoints - so the number beside an interval is how many devices an edit
    moves, and it belongs in the list rather than behind a click.
    """
    rows = (await session.execute(text("""
        SELECT p.id::text, p.name, p.interval_s, p.timeout_ms, p.retries,
               p.push_enabled, p.metric_groups,
               count(e.id) AS endpoints,
               count(e.id) FILTER (WHERE e.enabled) AS endpoints_enabled,
               -- Which planes it steers. A profile spanning two protocols is
               -- legal and worth seeing: metric_groups only reaches the SNMP
               -- adapter, so the same group set means nothing on the others.
               coalesce(array_agg(DISTINCT e.protocol::text)
                        FILTER (WHERE e.id IS NOT NULL), '{}') AS protocols
          FROM poll_profile p
          LEFT JOIN device_endpoint e ON e.poll_profile_id = p.id
         GROUP BY p.id, p.name, p.interval_s, p.timeout_ms, p.retries,
                  p.push_enabled, p.metric_groups
         ORDER BY p.interval_s, p.name
    """))).mappings().all()
    return [dict(r) for r in rows]


async def poll_profile_usage(session: AsyncSession, profile_id: str
                             ) -> list[dict[str, Any]]:
    """What an edit to this profile would move, broken down.

    "310 endpoints" is a number; "310 Redfish BMCs across 2 sites" is a
    decision. The breakdown is what makes the confirmation mean something.
    """
    rows = (await session.execute(text("""
        SELECT e.protocol::text AS protocol, d.device_type,
               count(*) AS endpoints,
               count(DISTINCT d.id) AS devices
          FROM device_endpoint e
          JOIN device d ON d.id = e.device_id
         WHERE e.poll_profile_id = CAST(:id AS uuid)
           AND d.lifecycle <> 'decommissioned'
         GROUP BY e.protocol, d.device_type
         ORDER BY count(*) DESC
    """), {"id": profile_id})).mappings().all()
    return [dict(r) for r in rows]


async def get_poll_profile_by_name(session: AsyncSession, name: str
                                   ) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        SELECT id::text, name FROM poll_profile WHERE name = CAST(:n AS text)
    """), {"n": name})).mappings().first()
    return dict(row) if row else None


async def create_poll_profile(session: AsyncSession, values: dict[str, Any]
                              ) -> dict[str, Any]:
    row = (await session.execute(text("""
        INSERT INTO poll_profile
            (name, interval_s, timeout_ms, retries, metric_groups, push_enabled)
        VALUES (CAST(:name AS text), CAST(:interval_s AS integer),
                CAST(:timeout_ms AS integer), CAST(:retries AS integer),
                CAST(:metric_groups AS text[]), CAST(:push_enabled AS boolean))
        RETURNING id::text, name, interval_s, timeout_ms, retries,
                  metric_groups, push_enabled
    """), values)).mappings().one()
    return dict(row)


#: Columns an edit may touch. `name` is absent on purpose - see update below.
_PROFILE_EDITABLE = {
    "interval_s":    "interval_s = CAST(:interval_s AS integer)",
    "timeout_ms":    "timeout_ms = CAST(:timeout_ms AS integer)",
    "retries":       "retries = CAST(:retries AS integer)",
    "metric_groups": "metric_groups = CAST(:metric_groups AS text[])",
    "push_enabled":  "push_enabled = CAST(:push_enabled AS boolean)",
}


async def update_poll_profile(session: AsyncSession, profile_id: str,
                              changes: dict[str, Any]) -> dict[str, Any] | None:
    """Apply an edit, and touch every endpoint that follows this profile.

    The endpoints are what the assignment version is built from. Editing the
    profile alone changes what each endpoint SERVES without changing any
    endpoint row, and a version-only comparison would answer 304 to every
    collector - they would keep polling on the old interval until something
    unrelated was edited. Bumping the endpoints is how the change is delivered.
    """
    if not changes:
        return await get_poll_profile(session, profile_id)
    sets = ", ".join(_PROFILE_EDITABLE[k] for k in changes)
    await session.execute(text(f"""
        UPDATE poll_profile SET {sets} WHERE id = CAST(:id AS uuid)
    """), {"id": profile_id, **changes})
    await session.execute(text("""
        UPDATE device_endpoint SET updated_at = now()
         WHERE poll_profile_id = CAST(:id AS uuid)
    """), {"id": profile_id})
    return await get_poll_profile(session, profile_id)


async def list_interfaces(session: AsyncSession, device_id: str) -> list[dict[str, Any]]:
    # What the port IS, and what it is plugged into. The second half is the
    # half that makes a port row actionable: four identical idle NICs and one
    # patched to a leaf switch look the same until the cable is named, and
    # "which of these can I reuse" is the question somebody opens this for.
    #
    # A port carries one cable - the uniqueness constraints on the termination
    # columns say so - which is why this joins without fanning the row out.
    # Matching on either end because a cable does not know which of its two
    # devices you are looking at.
    rows = (await session.execute(text("""
        SELECT i.id::text, i.if_index, i.name, i.role, i.speed_bps,
               host(i.ip) AS ip, i.mac::text AS mac,
               i.admin_state::text AS admin_state,
               c.layer::text AS peer_layer,
               pd.id::text   AS peer_device_id,
               pd.name       AS peer_device,
               pi.name       AS peer_port
          FROM interface i
          LEFT JOIN connection c
                 ON (c.a_termination_type = 'interface' AND c.a_termination_id = i.id)
                 OR (c.b_termination_type = 'interface' AND c.b_termination_id = i.id)
          LEFT JOIN device pd
                 ON pd.id = CASE WHEN c.a_termination_id = i.id
                                 THEN c.b_device_id ELSE c.a_device_id END
          LEFT JOIN interface pi
                 ON pi.id = CASE WHEN c.a_termination_id = i.id
                                 THEN c.b_termination_id ELSE c.a_termination_id END
         WHERE i.device_id = CAST(:id AS uuid)
         ORDER BY i.if_index NULLS LAST, i.name
    """), {"id": device_id})).mappings().all()
    return [dict(r) for r in rows]


async def get_credential(session: AsyncSession, credential_id: str
                         ) -> dict[str, Any] | None:
    """Credential metadata for validation. The secret stays where it is."""
    row = (await session.execute(text("""
        SELECT id::text, name, protocol::text AS protocol, kind, secret_hint
          FROM credential WHERE id = CAST(:id AS uuid)
    """), {"id": credential_id})).mappings().first()
    return dict(row) if row else None


async def get_poll_profile(session: AsyncSession, profile_id: str
                           ) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        SELECT id::text, name, interval_s, timeout_ms, retries, push_enabled
          FROM poll_profile WHERE id = CAST(:id AS uuid)
    """), {"id": profile_id})).mappings().first()
    return dict(row) if row else None
