"""Collector upgrades: one member per pool at a time (docs/26 Phase 7).

Datadog's HA-pair rationale, which the plan adopts: never take a pool's last
healthy member down to upgrade it, and never start the next member until the
previous one is back, healthy, on the new version. A pool with one member has
no partner to cover it - upgrading it is a short outage, allowed because the
alternative is never upgrading it, and the UI says so.

`decide` is pure - the whole policy, testable against any state. `tick` gathers
the state, applies the decisions, and runs in the ingest worker beside the
assigner under its own advisory lock, so two workers never issue the same
command twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.repositories import commands as repo

log = get_logger("rollout")

ADVISORY_LOCK_KEY = 0x44_43_49_4D_37  # "DCIM7"

#: A member just upgraded must stay healthy this long, on the new version,
#: before the next member of its pool is touched - long enough that a build
#: which starts and then falls over is caught on the first member, not after
#: it has been rolled through the pool.
SETTLE_S = 60.0

#: A heartbeat older than this is a member that is not healthy right now.
HEALTHY_WITHIN_S = 60.0


@dataclass
class Member:
    collector_id: str
    pool_id: str
    version: str | None
    heartbeat_age_s: float | None
    healthy_duration_s: float | None
    state: str = "active"

    @property
    def healthy(self) -> bool:
        return self.heartbeat_age_s is not None and self.heartbeat_age_s < HEALTHY_WITHIN_S


@dataclass
class Decision:
    upgrade: list[str] = field(default_factory=list)   # collector ids to command now
    done: bool = False
    failed: str | None = None
    waiting: dict[str, str] = field(default_factory=dict)  # pool -> why


def decide(target: str, pool_ids: list[str], members: list[Member],
           commands: list[dict[str, Any]]) -> Decision:
    """What one rollout should do now. `commands` are this rollout's own
    commands (any state); `members` are the active collectors of its pools."""
    d = Decision()
    for c in commands:
        if c["state"] in ("failed", "expired"):
            detail = (c.get("result") or {}).get("detail") or c["state"]
            d.failed = f"{c['collector_id']}: {detail}"
            return d
    open_by = {c["collector_id"] for c in commands if c["state"] in ("pending", "delivered")}
    finished = {c["collector_id"] for c in commands if c["state"] == "succeeded"}

    pools_done = 0
    for pool_id in pool_ids:
        mine = sorted((m for m in members if m.pool_id == pool_id and m.state == "active"),
                      key=lambda m: m.collector_id)
        if not mine:
            d.waiting[pool_id] = "no active member"
            continue
        busy = [m for m in mine if m.collector_id in open_by]
        if busy:
            d.waiting[pool_id] = f"{busy[0].collector_id} is upgrading"
            continue
        # The member most recently finished must have settled on the target.
        unsettled = [m for m in mine if m.collector_id in finished and (
            m.version != target or not m.healthy
            or (m.healthy_duration_s is not None and m.healthy_duration_s < SETTLE_S))]
        if unsettled:
            d.waiting[pool_id] = f"{unsettled[0].collector_id} settling on {target}"
            continue
        remaining = [m for m in mine if m.version != target]
        if not remaining:
            pools_done += 1
            continue
        candidate = remaining[0]
        others = [m for m in mine if m.collector_id != candidate.collector_id]
        if not candidate.healthy:
            d.waiting[pool_id] = f"{candidate.collector_id} is not heartbeating"
            continue
        sick = [m for m in others if not m.healthy]
        if sick:
            # Never take a pool's last healthy member down.
            d.waiting[pool_id] = f"{sick[0].collector_id} is unhealthy - not upgrading its partner"
            continue
        d.upgrade.append(candidate.collector_id)
    d.done = pools_done == len(pool_ids) and not d.upgrade
    return d


async def _members(session: AsyncSession, pool_ids: list[str]) -> list[Member]:
    rows = (await session.execute(text("""
        SELECT id, pool_id::text AS pool_id, version, state,
               extract(epoch FROM (clock_timestamp() - last_heartbeat)) AS hb,
               extract(epoch FROM (clock_timestamp() - healthy_since)) AS healthy_s
          FROM collector_instance
         WHERE pool_id = ANY(CAST(:pools AS uuid[])) AND state <> 'decommissioned'
    """), {"pools": pool_ids})).mappings().all()
    return [Member(collector_id=r["id"], pool_id=r["pool_id"], version=r["version"],
                   state=r["state"],
                   heartbeat_age_s=float(r["hb"]) if r["hb"] is not None else None,
                   healthy_duration_s=float(r["healthy_s"]) if r["healthy_s"] is not None else None)
            for r in rows]


def upgrade_payload(release: dict[str, Any]) -> dict[str, Any]:
    return {"version": release["version"], "sha256": release["sha256"],
            "signature": release["signature"], "key_id": release["key_id"],
            "size_bytes": release["size_bytes"],
            "url": f"/api/v1/collector/releases/{release['version']}/artifact"}


async def tick(session: AsyncSession) -> int:
    """Advance every running rollout one step. Returns commands issued."""
    got = (await session.execute(text("SELECT pg_try_advisory_xact_lock(:k)"),
                                 {"k": ADVISORY_LOCK_KEY})).scalar()
    if not got:
        return 0
    await repo.expire_stale(session)
    issued = 0
    for r in await repo.rollouts(session, running_only=True):
        release = await repo.get_release(session, r["version"])
        if release is None:
            await repo.set_rollout_state(session, r["id"], "failed", "release no longer exists")
            continue
        d = decide(r["version"], r["pool_ids"], await _members(session, r["pool_ids"]),
                   await repo.rollout_commands(session, r["id"]))
        if d.failed:
            await repo.set_rollout_state(session, r["id"], "failed", d.failed)
            log.warning("rollout failed", rollout_id=r["id"], detail=d.failed)
            continue
        if d.done:
            # An explicit detail: COALESCE would otherwise keep the last
            # "<member> is upgrading" line on a finished rollout.
            await repo.set_rollout_state(session, r["id"], "succeeded",
                                         f"every member runs {r['version']}")
            log.info("rollout succeeded", rollout_id=r["id"], version=r["version"])
            continue
        for cid in d.upgrade:
            await repo.create(session, cid, "upgrade", upgrade_payload(release),
                              f"rollout:{r['id']}", rollout_id=r["id"])
            issued += 1
            log.info("rollout upgrading", rollout_id=r["id"], collector_id=cid,
                     version=r["version"])
        if d.waiting:
            await repo.set_rollout_state(session, r["id"], "running",
                                         "; ".join(f"{v}" for v in d.waiting.values()))
    return issued
