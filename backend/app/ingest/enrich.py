"""Inventory cache used to enrich raw telemetry.

The collector deliberately knows nothing about racks or rooms, so enrichment
happens here. At a few thousand devices this cache is a few hundred kilobytes
and belongs in-process; round-tripping to Redis per sample would dominate the
ingest cost.

Invalidation is push (a Redis pub/sub message on inventory change) with a
periodic refresh as a backstop, because a missed invalidation must not mean
permanently stale enrichment.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

log = get_logger(__name__)

REFRESH_INTERVAL_S = 60.0


@dataclass(frozen=True, slots=True)
class DeviceContext:
    device_id: str
    name: str
    device_type: str
    vendor: str | None
    model: str | None
    rack_id: str | None
    rack_name: str | None
    row_name: str | None
    room_id: str | None
    room_name: str | None
    datacenter_id: str | None
    datacenter_code: str | None


@dataclass(frozen=True, slots=True)
class EndpointContext:
    endpoint_id: str
    device_id: str
    protocol: str
    role: str


@dataclass
class InventoryCache:
    devices: dict[str, DeviceContext] = field(default_factory=dict)
    endpoints: dict[str, EndpointContext] = field(default_factory=dict)
    metric_ids: dict[str, int] = field(default_factory=dict)
    hot_metrics: frozenset[str] = frozenset()
    loaded_at: float = 0.0
    _misses: int = 0

    def is_stale(self) -> bool:
        return (time.monotonic() - self.loaded_at) > REFRESH_INTERVAL_S

    async def refresh(self, session: AsyncSession) -> None:
        rows = (await session.execute(text("""
            SELECT d.id::text        AS device_id,
                   d.name, d.device_type,
                   v.name            AS vendor,
                   m.name            AS model,
                   r.id::text        AS rack_id,   r.name  AS rack_name,
                   rr.name           AS row_name,
                   rm.id::text       AS room_id,   rm.name AS room_name,
                   dc.id::text       AS datacenter_id, dc.code AS datacenter_code
            FROM device d
            LEFT JOIN vendor v      ON v.id = d.vendor_id
            LEFT JOIN model m       ON m.id = d.model_id
            LEFT JOIN rack r        ON r.id = d.rack_id
            LEFT JOIN rack_row rr   ON rr.id = r.row_id
            LEFT JOIN room rm       ON rm.id = COALESCE(rr.room_id, d.room_id)
            LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
            WHERE d.lifecycle <> 'decommissioned'
        """))).mappings().all()
        self.devices = {r["device_id"]: DeviceContext(**r) for r in rows}

        eps = (await session.execute(text("""
            SELECT id::text AS endpoint_id, device_id::text AS device_id,
                   protocol::text AS protocol, role::text AS role
            FROM device_endpoint WHERE enabled
        """))).mappings().all()
        self.endpoints = {r["endpoint_id"]: EndpointContext(**r) for r in eps}

        mets = (await session.execute(text(
            "SELECT id, key, is_hot FROM metric WHERE deprecated_at IS NULL"
        ))).mappings().all()
        self.metric_ids = {r["key"]: r["id"] for r in mets}
        self.hot_metrics = frozenset(r["key"] for r in mets if r["is_hot"])

        self.loaded_at = time.monotonic()
        log.info("inventory cache refreshed", devices=len(self.devices),
                 endpoints=len(self.endpoints), metrics=len(self.metric_ids))

    async def device(self, device_id: str, session: AsyncSession) -> DeviceContext | None:
        ctx = self.devices.get(device_id)
        if ctx is not None:
            return ctx
        # A persistent miss means the collector is polling something inventory
        # does not know about, which is itself worth surfacing.
        self._misses += 1
        if self._misses % 100 == 1:
            log.warning("inventory cache miss", device_id=device_id, misses=self._misses)
        if self.is_stale():
            await self.refresh(session)
            return self.devices.get(device_id)
        return None

    def metric_id(self, key: str) -> int | None:
        return self.metric_ids.get(key)
