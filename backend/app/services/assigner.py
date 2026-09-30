"""The single loop that writes endpoint_assignment (docs/26 Phase 5, HA
policy added in Phase 6).

Before this, `build_assignment` and the ingest worker's `OwnershipGuard`
each independently recomputed `sharding.plan()` on their own schedule -
correct in isolation, but with more than one API or ingest-worker process
each held its own snapshot, computed a few seconds apart from every other
one's. Nothing anywhere recorded a stable answer to "who owns this
endpoint" that a trap handler, an audit log, or a second process could
agree with.

This is the one place that plan is actually decided and written.
Everything else - `build_assignment`, `OwnershipGuard`, the visibility
sweep, the pools and shard pages - reads it through `collector.ownership`
(`sharding.effective`): a pin, else this record while it is still valid,
else the live plan for what the record does not cover yet. (Until
2026-09-30 `build_assignment` recomputed the live plan instead, despite this
paragraph, so neither the damping nor HA failover ever reached a collector.)

Advisory-locked (`pg_try_advisory_xact_lock`, released automatically at
commit/rollback) so that a deployment running more than one ingest worker -
already the documented default, see feedback_ingest_needs_two_workers - has
only one of them actually write a given tick; the others find the lock held
and skip, which is the correct outcome, not a failure to retry.

docs/26 Phase 6: `sharding.apply_ha_policy` runs on the fleet BEFORE `plan`/
`rebalance` ever see it, turning a sustained-stale member's `accepting`
False for the HA pools (`min_members >= 2`) an operator has actually opted
into - every other pool is completely unaffected, exactly Phase 0's
original "failover is not automatic" design. A pool under an active
change-freeze blackout (migration 0084) is passed through unchanged too -
`frozen_pools` below.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.repositories import collector as repo
from app.repositories import discovery_ranges as discovery_repo
from app.services import sharding
from app.services.collector import fleet

log = get_logger("assigner")

# Arbitrary but fixed - the same 63-bit key space every pg_advisory_lock
# caller shares, so it is namespaced with a distinctive constant rather than
# a small number some other feature might also pick.
ADVISORY_LOCK_KEY = 0x44_43_49_4D_35  # "DCIM5" in ASCII, as a single int


@dataclass
class AssignerResult:
    ran: bool
    moved: int = 0
    unassigned: int = 0


async def run(session: AsyncSession) -> AssignerResult:
    got_lock = (await session.execute(
        text("SELECT pg_try_advisory_xact_lock(:key)"),
        {"key": ADVISORY_LOCK_KEY})).scalar()
    if not got_lock:
        return AssignerResult(ran=False)

    raw_collectors = await fleet(session)
    endpoints = await repo.ownable_endpoints(session)
    current = await repo.current_assignment(session)

    if not raw_collectors:
        # Nothing can own anything right now. Writing every endpoint to
        # None here would be indistinguishable from a real pool_empty
        # state and would need a reason on rows that used to have a real
        # owner - safer to leave endpoint_assignment exactly as it was
        # until a collector actually exists to plan against.
        return AssignerResult(ran=True)

    pool_rows = await repo.pools(session)
    pool_min_members = {p["id"]: p["min_members"] for p in pool_rows}
    pool_datacenter = {p["id"]: p["datacenter_id"] for p in pool_rows}
    blackouts = await discovery_repo.active_blackouts(session)
    frozen_datacenters = {b["datacenter_id"] for b in blackouts if b["datacenter_id"]}
    # An estate-wide blackout (datacenter_id NULL) freezes every pool, HA or
    # not - the same "everything, including a one-off subnet with no site"
    # rule discovery_ranges.frozen_by already applies.
    estate_wide_freeze = any(b["datacenter_id"] is None for b in blackouts)
    frozen_pools = frozenset(pool_min_members) if estate_wide_freeze else frozenset(
        pid for pid, dc in pool_datacenter.items() if dc in frozen_datacenters)

    collectors = sharding.apply_ha_policy(raw_collectors, pool_min_members, frozen_pools)

    target = sharding.plan(endpoints, collectors)
    result = sharding.rebalance(current, target, endpoints, collectors)

    raw_by_id = {c.collector_id: c for c in raw_collectors}
    endpoints_by_id = {str(e["id"]): e for e in endpoints}
    changes: dict[str, tuple[str | None, str]] = {}
    for eid, new_owner in result.items():
        old_owner = current.get(eid)
        if old_owner == new_owner:
            continue
        changes[eid] = (new_owner, _reason(old_owner, new_owner,
                                          endpoints_by_id.get(eid) or {}, raw_by_id))

    await repo.write_assignment(session, changes)
    # Bounded history (migration 0093). Indexed on `at`, so a tick with
    # nothing old enough to prune costs one index probe.
    await repo.prune_assignment_history(session)
    unassigned = sum(1 for owner in result.values() if owner is None)
    log.info("assigner ran", moved=len(changes), unassigned=unassigned,
             endpoints=len(endpoints), collectors=len(collectors),
             frozen_pools=len(frozen_pools))
    return AssignerResult(ran=True, moved=len(changes), unassigned=unassigned)


def _reason(old_owner: str | None, new_owner: str | None,
           endpoint: dict, raw_by_id: dict[str, sharding.Collector]) -> str:
    """raw_by_id is the fleet BEFORE apply_ha_policy - the only way to tell
    an operator's own drain (accepting was already False) apart from an
    automatic failover (accepting was True; HA policy is what flipped it,
    because the heartbeat itself went stale past FAILOVER_AFTER_S)."""
    if new_owner is None:
        return "pool_empty"
    pinned = endpoint.get("collector_id")
    if pinned and pinned == new_owner:
        return "pin"
    if old_owner is None:
        return "initial"
    old = raw_by_id.get(old_owner)
    if old is None or not old.accepting:
        return "drain"
    if old.heartbeat_age_s is not None and old.heartbeat_age_s > sharding.FAILOVER_AFTER_S:
        return "failover"
    return "rebalance"
