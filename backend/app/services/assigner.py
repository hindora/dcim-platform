"""The single loop that writes endpoint_assignment (docs/26 Phase 5).

Before this, `build_assignment` and the ingest worker's `OwnershipGuard`
each independently recomputed `sharding.plan()` on their own schedule -
correct in isolation, but with more than one API or ingest-worker process
each held its own snapshot, computed a few seconds apart from every other
one's. Nothing anywhere recorded a stable answer to "who owns this
endpoint" that a trap handler, an audit log, or a second process could
agree with.

This is the one place that plan is actually decided and written.
Everything else - `build_assignment`, `OwnershipGuard`, the shard summary -
reads `endpoint_assignment` instead of recomputing anything.

Advisory-locked (`pg_try_advisory_xact_lock`, released automatically at
commit/rollback) so that a deployment running more than one ingest worker -
already the documented default, see feedback_ingest_needs_two_workers - has
only one of them actually write a given tick; the others find the lock held
and skip, which is the correct outcome, not a failure to retry.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.repositories import collector as repo
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

    collectors = await fleet(session)
    endpoints = await repo.ownable_endpoints(session)
    current = await repo.current_assignment(session)

    if not collectors:
        # Nothing can own anything right now. Writing every endpoint to
        # None here would be indistinguishable from a real pool_empty
        # state and would need a reason on rows that used to have a real
        # owner - safer to leave endpoint_assignment exactly as it was
        # until a collector actually exists to plan against.
        return AssignerResult(ran=True)

    target = sharding.plan(endpoints, collectors)
    result = sharding.rebalance(current, target, endpoints, collectors)

    collectors_by_id = {c.collector_id: c for c in collectors}
    endpoints_by_id = {str(e["id"]): e for e in endpoints}
    changes: dict[str, tuple[str | None, str]] = {}
    for eid, new_owner in result.items():
        old_owner = current.get(eid)
        if old_owner == new_owner:
            continue
        changes[eid] = (new_owner, _reason(eid, old_owner, new_owner,
                                          endpoints_by_id.get(eid) or {},
                                          collectors_by_id))

    await repo.write_assignment(session, changes)
    unassigned = sum(1 for owner in result.values() if owner is None)
    log.info("assigner ran", moved=len(changes), unassigned=unassigned,
             endpoints=len(endpoints), collectors=len(collectors))
    return AssignerResult(ran=True, moved=len(changes), unassigned=unassigned)


def _reason(eid: str, old_owner: str | None, new_owner: str | None,
           endpoint: dict, collectors_by_id: dict[str, sharding.Collector]) -> str:
    if new_owner is None:
        return "pool_empty"
    pinned = endpoint.get("collector_id")
    if pinned and pinned == new_owner:
        return "pin"
    if old_owner is None:
        return "initial"
    old = collectors_by_id.get(old_owner)
    if old is None or not old.healthy or not old.accepting:
        return "drain"
    return "rebalance"
