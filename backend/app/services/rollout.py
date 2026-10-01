"""Collector upgrades: one member per pool at a time (docs/26 Phase 7).

Datadog's HA-pair rationale, which the plan adopts: never take a pool's last
healthy member down to upgrade it, and never start the next member until the
previous one is back, healthy, on the new version. A pool with one member has
no partner to cover it - upgrading it is a short outage, allowed because the
alternative is never upgrading it, and the UI says so.

A member with a partner is DRAINED before it is touched, as every HA pair is
in practice (Zabbix proxy groups, Datadog HA agents, a load balancer pulling
a node): its endpoints move to the partner first, and only once it owns
nothing - plus a hand-off window for the partner to fetch and poll them - is
the upgrade sent. A planned upgrade therefore never waits on failure
detection, so a broken build costs the pool nothing: the first live run of a
broken release without this left 98 endpoints UNKNOWN for ~100 s while the
180 s failover caught up. Once the upgrade confirms, the member is put back
and the assigner returns its share.

The rollout marks its own drain (`state_changed_by = 'rollout:<id>'`), so an
operator's drain is never undone by it, and a member it drained is returned
by `_restore_orphans` whatever ends the rollout - success, failure, cancel -
but only once that member heartbeats: an "active" member that is dead would
be handed work for up to the 180 s failover window.

`decide` is pure - the whole policy, testable against any state. `tick` gathers
the state, applies the decisions, and runs in the ingest worker beside the
assigner under its own advisory lock, so two workers never issue the same
command twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.repositories import collector as fleet_repo
from app.repositories import commands as repo
from app.services import assigner
from app.services.collector import ownership

log = get_logger("rollout")

ADVISORY_LOCK_KEY = 0x44_43_49_4D_37  # "DCIM7"

#: A member just upgraded must stay healthy this long after its upgrade
#: confirmed, on the new version, before the next member of its pool is
#: touched - long enough that a build which starts and then falls over is
#: caught on the first member, not after it has been rolled through the pool.
#: Measured from the command's finish, not healthy_since: a re-exec is a gap
#: of seconds, which never restarts the healthy streak.
SETTLE_S = 60.0

#: After a drained member owns nothing, how long before it is upgraded: the
#: partner learns of the move on its long-poll in about a second, but a
#: partner between polls falls back to its 30 s assignment fetch, then polls
#: a handed-off endpoint within 15 s. Owning nothing in the record is not yet
#: the partner polling it.
HANDOFF_S = 45.0

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
    #: Who last changed `state` - "rollout:<id>" for a rollout's own drain.
    state_by: str | None = None
    #: Seconds since `state` last changed.
    state_age_s: float | None = None
    #: Endpoints the serving path still gives it.
    owned: int = 0

    @property
    def healthy(self) -> bool:
        return self.heartbeat_age_s is not None and self.heartbeat_age_s < HEALTHY_WITHIN_S


@dataclass
class Decision:
    upgrade: list[str] = field(default_factory=list)   # collector ids to command now
    drain: list[str] = field(default_factory=list)     # hand their work to a partner first
    restore: list[str] = field(default_factory=list)   # this rollout's drains to put back
    done: bool = False
    failed: str | None = None
    waiting: dict[str, str] = field(default_factory=dict)  # pool -> why


def _age(ts: Any, now: datetime) -> float | None:
    return (now - ts).total_seconds() if ts is not None else None


def decide(target: str, pool_ids: list[str], members: list[Member],
           commands: list[dict[str, Any]], rollout_id: str = "",
           now: datetime | None = None) -> Decision:
    """What one rollout should do now. `commands` are this rollout's own
    commands (any state); `members` are its pools' collectors, the ones it
    drained itself included."""
    now = now or datetime.now(UTC)
    tag = f"rollout:{rollout_id}"
    d = Decision()
    for c in commands:
        if c["state"] in ("failed", "expired"):
            detail = (c.get("result") or {}).get("detail") or c["state"]
            d.failed = f"{c['collector_id']}: {detail}"
            return d
    open_by = {c["collector_id"] for c in commands if c["state"] in ("pending", "delivered")}
    finished = {c["collector_id"]: _age(c.get("finished_at"), now)
                for c in commands if c["state"] == "succeeded"}

    pools_done = 0
    for pool_id in pool_ids:
        mine = sorted((m for m in members if m.pool_id == pool_id and (
                          m.state == "active" or (m.state == "draining" and m.state_by == tag))),
                      key=lambda m: m.collector_id)
        if not mine:
            d.waiting[pool_id] = "no active member"
            continue
        busy = [m for m in mine if m.collector_id in open_by]
        if busy:
            d.waiting[pool_id] = f"{busy[0].collector_id} is upgrading"
            continue
        ours = [m for m in mine if m.state == "draining"]
        done_ours = [m for m in ours if m.collector_id in finished]
        if done_ours:
            m = done_ours[0]
            if m.version == target and m.healthy:
                d.restore.append(m.collector_id)
                d.waiting[pool_id] = f"returning {m.collector_id}'s endpoints"
            else:
                d.waiting[pool_id] = f"{m.collector_id} settling on {target}"
            continue
        if ours:
            m = ours[0]
            partners = [o for o in mine if o.collector_id != m.collector_id]
            if not partners or not all(o.healthy for o in partners):
                # The partner carrying its work went sick before the upgrade
                # was sent: put it back untouched rather than leave the pool
                # resting on a sick member.
                d.restore.append(m.collector_id)
                d.waiting[pool_id] = (f"partner of {m.collector_id} is unhealthy - "
                                      f"returning it un-upgraded")
            elif m.owned > 0 or (m.state_age_s or 0.0) < HANDOFF_S:
                d.waiting[pool_id] = f"handing {m.collector_id}'s endpoints to its partner"
            else:
                d.upgrade.append(m.collector_id)
            continue
        # The member most recently finished must have settled on the target.
        unsettled = [m for m in mine if m.collector_id in finished and (
            m.version != target or not m.healthy
            or (finished[m.collector_id] or 0.0) < SETTLE_S)]
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
        if others:
            d.drain.append(candidate.collector_id)
            d.waiting[pool_id] = f"handing {candidate.collector_id}'s endpoints to its partner"
        else:
            d.upgrade.append(candidate.collector_id)
    d.done = pools_done == len(pool_ids) and not d.upgrade and not d.drain
    return d


async def _members(session: AsyncSession, pool_ids: list[str]) -> list[Member]:
    rows = (await session.execute(text("""
        SELECT id, pool_id::text AS pool_id, version, state, state_changed_by,
               extract(epoch FROM (clock_timestamp() - last_heartbeat)) AS hb,
               extract(epoch FROM (clock_timestamp() - healthy_since)) AS healthy_s,
               extract(epoch FROM (clock_timestamp() - state_changed_at)) AS state_s
          FROM collector_instance
         WHERE pool_id = ANY(CAST(:pools AS uuid[])) AND state <> 'decommissioned'
    """), {"pools": pool_ids})).mappings().all()
    owned: dict[str, int] = {}
    for owner in (await ownership(session)).values():
        if owner is not None:
            owned[owner] = owned.get(owner, 0) + 1

    def f(v: Any) -> float | None:
        return float(v) if v is not None else None
    return [Member(collector_id=r["id"], pool_id=r["pool_id"], version=r["version"],
                   state=r["state"], state_by=r["state_changed_by"],
                   state_age_s=f(r["state_s"]), owned=owned.get(r["id"], 0),
                   heartbeat_age_s=f(r["hb"]), healthy_duration_s=f(r["healthy_s"]))
            for r in rows]


async def _restore_orphans(session: AsyncSession) -> list[str]:
    """Return every member a rollout drained once that rollout is no longer
    running and the member has no command open - whatever ended it. Only a
    member heartbeating now: the failed one may still be crash-looping, and
    stays drained (owning nothing) until its rollback brings it back."""
    rows = (await session.execute(text("""
        SELECT ci.id
          FROM collector_instance ci
         WHERE ci.state = 'draining' AND ci.state_changed_by LIKE 'rollout:%'
           AND clock_timestamp() - ci.last_heartbeat < make_interval(secs => :fresh)
           AND NOT EXISTS (SELECT 1 FROM collector_rollout r
                            WHERE 'rollout:' || r.id::text = ci.state_changed_by
                              AND r.state = 'running')
           AND NOT EXISTS (SELECT 1 FROM collector_command c
                            WHERE c.collector_id = ci.id
                              AND c.state IN ('pending', 'delivered'))
    """), {"fresh": HEALTHY_WITHIN_S})).scalars().all()
    for cid in rows:
        await fleet_repo.set_state(session, cid, "active", "rollout")
        log.info("rollout returned a drained member", collector_id=cid)
    return list(rows)


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
    moved = False
    for r in await repo.rollouts(session, running_only=True):
        release = await repo.get_release(session, r["version"])
        if release is None:
            await repo.set_rollout_state(session, r["id"], "failed", "release no longer exists")
            continue
        tag = f"rollout:{r['id']}"
        d = decide(r["version"], r["pool_ids"], await _members(session, r["pool_ids"]),
                   await repo.rollout_commands(session, r["id"]), rollout_id=r["id"])
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
        for cid in d.drain:
            await fleet_repo.set_state(session, cid, "draining", tag)
            moved = True
            log.info("rollout draining before upgrade", rollout_id=r["id"], collector_id=cid)
        for cid in d.restore:
            await fleet_repo.set_state(session, cid, "active", tag)
            moved = True
            log.info("rollout returning member", rollout_id=r["id"], collector_id=cid)
        for cid in d.upgrade:
            await repo.create(session, cid, "upgrade", upgrade_payload(release),
                              tag, rollout_id=r["id"])
            issued += 1
            log.info("rollout upgrading", rollout_id=r["id"], collector_id=cid,
                     version=r["version"])
        if d.waiting:
            await repo.set_rollout_state(session, r["id"], "running",
                                         "; ".join(f"{v}" for v in d.waiting.values()))
    if await _restore_orphans(session):
        moved = True
    if moved:
        # Move the record now, not on the worker's next 30 s tick: the drained
        # member's hand-off window starts from here.
        await assigner.run(session)
    return issued
