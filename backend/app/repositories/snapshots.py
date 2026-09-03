"""The nightly asset snapshot: taking it, and reading trends back out of it.

`take` is idempotent on the day - INSERT .. ON CONFLICT (day) DO NOTHING - so
two ingest workers, a restart mid-tick and a manual run all collapse into one
row. A snapshot that could land twice would show a day disagreeing with itself.

Movements between states are NOT here. Installs and decommissions come from
`device_lifecycle_event`, because a snapshot diff conflates them: a day with 10
installs and 10 decommissions nets to zero and the activity vanishes.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.assets import LIVE_LIFECYCLES
from app.repositories.contracts import EXPIRING_DAYS


async def take(session: AsyncSession) -> bool:
    """Record today, if today is not already recorded.

    One statement, so the counts inside it are one consistent read - a snapshot
    assembled from several queries could count a device that moved between them
    twice, or not at all.
    """
    result = await session.execute(text("""
        INSERT INTO asset_snapshot (
            day, devices, planned, in_stock, installed, in_service,
            maintenance, decommissioned, retired,
            racks, u_total, u_used, u_held,
            with_serial, with_asset_tag,
            cover_active, cover_expiring, cover_expired, cover_unknown)
        SELECT CURRENT_DATE,
               count(*),
               count(*) FILTER (WHERE lifecycle = 'planned'),
               count(*) FILTER (WHERE lifecycle = 'in_stock'),
               count(*) FILTER (WHERE lifecycle = 'installed'),
               count(*) FILTER (WHERE lifecycle = 'in_service'),
               count(*) FILTER (WHERE lifecycle = 'maintenance'),
               count(*) FILTER (WHERE lifecycle = 'decommissioned'),
               count(*) FILTER (WHERE lifecycle = 'retired'),
               (SELECT count(*) FROM rack),
               (SELECT COALESCE(sum(u_height), 0) FROM rack),
               COALESCE(sum(u_height) FILTER (
                   WHERE rack_id IS NOT NULL AND u_start IS NOT NULL
                     AND lifecycle::text = ANY(:live)), 0),
               COALESCE(sum(u_height) FILTER (
                   WHERE rack_id IS NOT NULL AND u_start IS NOT NULL
                     AND lifecycle = 'planned'), 0),
               count(serial_number),
               count(asset_tag),
               count(*) FILTER (WHERE warranty_expires
                   > CURRENT_DATE + CAST(:expiring AS integer)
                   AND lifecycle NOT IN ('decommissioned', 'retired')),
               count(*) FILTER (WHERE warranty_expires >= CURRENT_DATE
                   AND warranty_expires <= CURRENT_DATE + CAST(:expiring AS integer)
                   AND lifecycle NOT IN ('decommissioned', 'retired')),
               count(*) FILTER (WHERE warranty_expires < CURRENT_DATE
                   AND lifecycle NOT IN ('decommissioned', 'retired')),
               count(*) FILTER (WHERE warranty_expires IS NULL
                   AND lifecycle NOT IN ('decommissioned', 'retired'))
        FROM device
        ON CONFLICT (day) DO NOTHING
    """), {"live": list(LIVE_LIFECYCLES), "expiring": EXPIRING_DAYS})
    return bool(result.rowcount)


async def series(session: AsyncSession, days: int = 90) -> list[dict[str, Any]]:
    """The trend, oldest first - the order a line is drawn in."""
    rows = (await session.execute(text("""
        SELECT day::text, devices, in_service, planned, in_stock, installed,
               maintenance, decommissioned, retired,
               racks, u_total, u_used, u_held,
               (u_total - u_used - u_held) AS u_free,
               with_serial, with_asset_tag,
               cover_active, cover_expiring, cover_expired, cover_unknown
        FROM asset_snapshot
        WHERE day > CURRENT_DATE - CAST(:days AS integer)
        ORDER BY day
    """), {"days": days})).mappings().all()
    return [dict(r) for r in rows]


async def lifecycle_activity(session: AsyncSession,
                             months: int = 12) -> list[dict[str, Any]]:
    """Installs and decommissions per month, from the events themselves.

    An install is a transition INTO service or the rack (in_service,
    installed); a decommission is the transition out. Reversals inside one
    month both count, which is the point of reading events rather than
    differencing snapshots.
    """
    rows = (await session.execute(text("""
        SELECT to_char(date_trunc('month', ts), 'YYYY-MM') AS month,
               count(*) FILTER (WHERE to_state IN ('in_service', 'installed')
                                  AND (from_state IS NULL
                                       OR from_state NOT IN ('in_service',
                                                             'installed',
                                                             'maintenance')))
                   AS installs,
               count(*) FILTER (WHERE to_state = 'decommissioned') AS decommissions
        FROM device_lifecycle_event
        WHERE ts > CURRENT_DATE - CAST(:months AS integer) * INTERVAL '1 month'
        GROUP BY 1
        ORDER BY 1
    """), {"months": months})).mappings().all()
    return [dict(r) for r in rows]
