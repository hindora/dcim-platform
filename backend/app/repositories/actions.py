"""SQL for device actions: which outlet feeds what, which collector owns a
device's endpoint, and the action commands themselves."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: An action nobody has answered in this long is expired. Much shorter than the
#: 900 s an upgrade is given: a person is watching a button, and an outlet
#: command that lands minutes later is worse than one that visibly failed.
ACTION_TIMEOUT_S = 45


async def device_basics(session: AsyncSession, device_id: str) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        SELECT d.id::text, d.name, d.device_type::text AS device_type,
               d.lifecycle::text AS lifecycle,
               v.name AS vendor, m.name AS model
          FROM device d
          LEFT JOIN vendor v ON v.id = d.vendor_id
          LEFT JOIN model m  ON m.id = d.model_id
         WHERE d.id = CAST(:id AS uuid)"""), {"id": device_id})).mappings().first()
    return dict(row) if row else None


async def endpoints_with_owner(session: AsyncSession, device_id: str) -> list[dict[str, Any]]:
    """Each endpoint and the collector that owns it: the recorded owner, else
    the one it is pinned to. Only that collector has the credential, and only
    it is on the network that reaches the device."""
    rows = (await session.execute(text("""
        SELECT e.id::text AS id, e.protocol::text AS protocol, e.role::text AS role,
               e.enabled, e.admin_state::text AS admin_state, e.addressing,
               e.write_credential_id::text AS write_credential_id,
               COALESCE(ea.collector_id, e.collector_id) AS owner
          FROM device_endpoint e
          LEFT JOIN endpoint_assignment ea ON ea.endpoint_id = e.id
         WHERE e.device_id = CAST(:id AS uuid)
         ORDER BY e.protocol, e.role"""), {"id": device_id})).mappings().all()
    return [dict(r) for r in rows]


async def outlet_loads(session: AsyncSession, pdu_id: str) -> list[dict[str, Any]]:
    """Every outlet on the strip and the device corded to it, if any, with the
    other cords of that device and the state of what feeds them - the facts
    that decide whether opening the outlet takes the load down or only takes
    away its redundancy."""
    outlets = (await session.execute(text("""
        SELECT o.number AS outlet, o.connector, o.branch, o.phase,
               ld.id::text AS load_id, ld.name AS load_name,
               ld.device_type::text AS load_type, ps.number AS psu
          FROM outlet o
          LEFT JOIN connection c ON c.a_termination_type = 'outlet'
                                AND c.a_termination_id = o.id
          LEFT JOIN power_supply ps ON c.b_termination_type = 'psu'
                                   AND ps.id = c.b_termination_id
          LEFT JOIN device ld ON ld.id = ps.device_id
         WHERE o.device_id = CAST(:id AS uuid)
         ORDER BY o.number"""), {"id": pdu_id})).mappings().all()
    rows = [dict(r) for r in outlets]
    load_ids = [r["load_id"] for r in rows if r["load_id"]]
    cords: dict[str, list[dict[str, Any]]] = {}
    if load_ids:
        feeds = (await session.execute(text("""
            SELECT ps.device_id::text AS load_id, ps.number AS psu,
                   fd.id::text AS feed_id, fd.name AS feed_name,
                   device_comm_status(fd.id, ds.status::text) AS feed_status
              FROM power_supply ps
              JOIN connection c ON c.b_termination_type = 'psu'
                               AND c.b_termination_id = ps.id
              JOIN device fd ON fd.id = c.a_device_id
              LEFT JOIN device_state ds ON ds.device_id = fd.id
             WHERE ps.device_id = ANY(CAST(:ids AS uuid[]))"""),
            {"ids": load_ids})).mappings().all()
        for f in feeds:
            cords.setdefault(f["load_id"], []).append(dict(f))
    for r in rows:
        r["cords"] = cords.get(r["load_id"], []) if r["load_id"] else []
    return rows


async def create_action_command(session: AsyncSession, *, collector_id: str,
                                payload: dict[str, Any], actor: str) -> str:
    from app.repositories import commands as cmd_repo
    return await cmd_repo.create(session, collector_id, "device_action", payload, actor)


async def open_action(session: AsyncSession, device_id: str) -> dict[str, Any] | None:
    """An action on this device still waiting for an answer, if any."""
    row = (await session.execute(text("""
        SELECT id::text, state, payload, created_by, created_at
          FROM collector_command
         WHERE kind = 'device_action' AND state IN ('pending', 'delivered')
           AND payload->>'device_id' = :dev
           AND created_at > now() - make_interval(secs => :t)
         ORDER BY created_at DESC LIMIT 1"""),
        {"dev": device_id, "t": ACTION_TIMEOUT_S})).mappings().first()
    return dict(row) if row else None


async def action_status(session: AsyncSession, device_id: str,
                        command_id: str) -> dict[str, Any] | None:
    """Expire what is overdue, then read the command back."""
    await session.execute(text("""
        UPDATE collector_command
           SET state = 'expired', finished_at = now(),
               result = jsonb_build_object(
                   'detail', 'the collector did not answer within ' || :t || ' s')
         WHERE kind = 'device_action' AND state IN ('pending', 'delivered')
           AND created_at < now() - make_interval(secs => :t)"""),
        {"t": ACTION_TIMEOUT_S})
    row = (await session.execute(text("""
        SELECT id::text, collector_id, state, payload, result, created_by,
               created_at, delivered_at, finished_at
          FROM collector_command
         WHERE id = CAST(:id AS uuid) AND kind = 'device_action'
           AND payload->>'device_id' = :dev"""),
        {"id": command_id, "dev": device_id})).mappings().first()
    return dict(row) if row else None


async def recent_actions(session: AsyncSession, device_id: str,
                         limit: int = 10) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT id::text, state, payload, result, created_by, created_at, finished_at
          FROM collector_command
         WHERE kind = 'device_action' AND payload->>'device_id' = :dev
         ORDER BY created_at DESC LIMIT :n"""),
        {"dev": device_id, "n": limit})).mappings().all()
    return [dict(r) for r in rows]
