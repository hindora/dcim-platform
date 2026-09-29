"""A collector that stops reporting takes its endpoints' visibility with it.

The failure this exists for is quiet. A dead collector sends nothing, and
nothing is exactly what every other check reads as "no change": its endpoints
kept the status they had at the moment it died, so a whole shard sat ONLINE on
every page while nothing polled it. docs/02 always said those endpoints go
UNKNOWN; nothing did it.

So when a collector is stale, every endpoint it owns becomes UNKNOWN with the
reason on the row - `last_error_class = 'collector_stale'` - and the device
rolls up from there. UNKNOWN, not OFFLINE: the platform has lost its view, it
has not learned the device is down, and an OFFLINE would page somebody about
1,500 servers when the fault is one VM. That is LogicMonitor's collector-down
behaviour, and the one alarm that does fire is `collector_stale`, which names
how many endpoints went with it.

The same reason is how the rest of the alarm engine knows to hold still. A
condition nothing can currently measure must not be cleared as recovered or
aged out as forgotten while the platform cannot see: `blind_endpoints` and
`blind_devices` are what the staleness and trap sweeps check before they clear.

Recovery needs nothing from here. The collector's next state report for each
endpoint - a transition, or the periodic refresh, which the ingest path now
promotes to a transition when it changes the stored status - overwrites the
UNKNOWN and re-derives the device.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.ingest import writer

log = get_logger("alarms.collector_visibility")

STALE_CLASS = "collector_stale"

# Two ingest workers each run the platform monitor. Only one of them may mark a
# shard at a time: the sweep locks hundreds of endpoint_state rows, and two
# copies taking them concurrently is a deadlock looking for a tick to happen in.
_LOCK_KEY = 0x636F6C76  # "colv"


async def blind_endpoints(session: AsyncSession) -> set[str]:
    """Endpoints whose collector has stopped reporting."""
    rows = (await session.execute(text("""
        SELECT endpoint_id::text FROM endpoint_state
         WHERE status = 'UNKNOWN' AND last_error_class = :cls
    """), {"cls": STALE_CLASS})).scalars().all()
    return set(rows)


async def blind_devices(session: AsyncSession) -> set[str]:
    """Devices with at least one endpoint nobody is currently polling.

    Any, not all: a trap alarm is raised through one endpoint and nothing says
    which the evidence would come from, so a device that is partly blind is
    treated as unable to prove a recovery.
    """
    rows = (await session.execute(text("""
        SELECT DISTINCT e.device_id::text
          FROM endpoint_state es
          JOIN device_endpoint e ON e.id = es.endpoint_id
         WHERE es.status = 'UNKNOWN' AND es.last_error_class = :cls
    """), {"cls": STALE_CLASS})).scalars().all()
    return set(rows)


async def mark_collector_status(session: AsyncSession, stale_after_s: float) -> None:
    """`status` says STALE once the heartbeat is, instead of HEALTHY forever.

    The heartbeat writes HEALTHY every time it arrives, and nothing ever wrote
    anything else - so a collector dead for a week still read HEALTHY in the
    table every page and export draws from.
    """
    await session.execute(text("""
        UPDATE collector_instance SET status = 'STALE'
         WHERE state <> 'decommissioned' AND status <> 'STALE'
           AND last_heartbeat < clock_timestamp() - make_interval(secs => :s)
    """), {"s": stale_after_s})


async def sweep(session: AsyncSession, stale_after_s: float) -> list[dict[str, Any]]:
    """Mark every endpoint owned by a stale collector UNKNOWN.

    Returns the devices whose status changed, for the websocket fan-out.
    """
    from app.services import collector as collectors

    got = (await session.execute(text("SELECT pg_try_advisory_xact_lock(:k)"),
                                 {"k": _LOCK_KEY})).scalar()
    if not got:
        return []

    await mark_collector_status(session, stale_after_s)

    fleet = await collectors.fleet(session)
    stale = {c.collector_id for c in fleet if not c.healthy}
    if not stale:
        return []

    plan = await collectors.ownership(session)
    by_owner: dict[str, list[str]] = {}
    for endpoint_id, owner in plan.items():
        if owner in stale:
            by_owner.setdefault(owner, []).append(endpoint_id)
    if not by_owner:
        return []

    # Locked in endpoint order first, which is the order the ingest path takes
    # them in: an UPDATE over an array locks in whatever order the planner
    # likes, and that is how a sweep and a state batch end up waiting on each
    # other.
    marked: list[str] = []
    for owner, endpoint_ids in sorted(by_owner.items()):
        locked = (await session.execute(text("""
            SELECT endpoint_id::text FROM endpoint_state
             WHERE endpoint_id = ANY(CAST(:ids AS uuid[]))
               AND NOT (status = 'UNKNOWN' AND last_error_class = :cls)
             ORDER BY endpoint_id
               FOR UPDATE
        """), {"ids": sorted(endpoint_ids), "cls": STALE_CLASS})).scalars().all()
        if not locked:
            continue
        await session.execute(text("""
            UPDATE endpoint_state
               SET status = 'UNKNOWN', last_error_class = :cls,
                   last_error = :msg, updated_at = now()
             WHERE endpoint_id = ANY(CAST(:ids AS uuid[]))
        """), {"ids": list(locked), "cls": STALE_CLASS,
               "msg": (f"collector {owner} has stopped reporting; nothing is "
                       f"polling this endpoint")})
        marked.extend(locked)
        log.warning("collector stale: endpoints marked unknown",
                    collector_id=owner, endpoints=len(locked))

    if not marked:
        return []

    # One endpoint per device is enough for the rollup, which aggregates over
    # every endpoint the device has. Device order, for the same lock-order
    # reason as above.
    devices = (await session.execute(text("""
        SELECT DISTINCT ON (e.device_id) e.device_id::text AS device_id,
               e.id::text AS endpoint_id
          FROM device_endpoint e
         WHERE e.id = ANY(CAST(:ids AS uuid[]))
         ORDER BY e.device_id, e.id
    """), {"ids": marked})).mappings().all()
    for d in devices:
        await writer.apply_device_status(
            session, {"endpoint_id": d["endpoint_id"], "is_refresh": False})

    rows = (await session.execute(text("""
        SELECT device_id::text AS device_id, status::text AS status
          FROM device_state WHERE device_id = ANY(CAST(:ids AS uuid[]))
    """), {"ids": [d["device_id"] for d in devices]})).mappings().all()
    return [dict(r) for r in rows]
