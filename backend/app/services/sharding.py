"""Splitting the fleet across collectors.

Three properties decide whether a sharding scheme is usable in production, and
only one of them is about balance.

**No overlap, ever.** Two collectors polling the same endpoint is not a
harmless duplicate: it doubles the load on the device, writes each sample
twice, and makes every counter rate wrong, because two independent pollers each
see a fraction of the increments. The current default - ``collector_id IS
NULL`` meaning "any collector may take it" - is safe with one collector and
silently catastrophic with two.

**Stability.** An endpoint that changes owner loses its counter baseline: the
new collector has never seen it, so the first poll yields no rate at all, and a
mishandled reset shows up as a throughput spike on a chart. Modulo hashing
moves nearly every endpoint when the collector count changes. Rendezvous
(highest-random-weight) hashing moves only the share that belongs to the
collector that joined or left - about 1/N - and leaves the rest exactly where
they were.

**Reachability before balance.** This is the part a pure hashing scheme gets
wrong. A collector can only poll what it can route to, and management networks
are per-site and frequently overlapping RFC1918 - 10.51.x.x in one datacenter
is a different network from 10.51.x.x in another. Real poller fleets assign by
site first and balance within it. An admin places each collector in the site
whose network it sits on (``collector_instance.datacenter_id``); one placed
nowhere is treated as serving all, which is the correct default for a
single-site deployment and the reason the existing single collector keeps
working unchanged. Placement is recorded centrally rather than declared by the
process, as Zabbix proxy-group membership is: a typo in a heartbeat must not be
able to re-shard the estate.

A draining collector is still a collector - it keeps anything pinned to it, and
it is not the same as a dead one - but it takes no share of the hash, so its
unpinned endpoints move to the other collectors in its site. That is how a host
is emptied ahead of maintenance without deleting its row.

Failover is deliberately NOT automatic. A collector that stops heartbeating
keeps its shard, and its endpoints go UNKNOWN - which is what the test strategy
specifies, and what an operator wants: a flapping collector would otherwise
cause repeated mass reassignment, and each reassignment resets the counter
baselines of everything that moved. Redistribution is available by passing
``exclude`` explicitly, so it is a decision someone makes rather than something
that happens at 3am.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Collector:
    """A collector that may own endpoints.

    ``pool_id`` (docs/26 Phase 5) is the finer-grained placement a
    ``collector_pool`` row gives - site AND plane, e.g. "DC1's BMS network"
    rather than just "DC1". Set, it is the WHOLE placement decision: a
    pool-placed collector serves that pool and nothing else, ``sites`` is
    not consulted at all. Unset, behaviour is exactly what it was before
    pools existed: ``sites`` (empty means "any") is the placement, the
    single-collector/no-pools-configured default this migration changes
    nothing about.

    ``accepting`` is False while it drains.
    """

    collector_id: str
    sites: frozenset[str] = field(default_factory=frozenset)
    pool_id: str | None = None
    healthy: bool = True
    accepting: bool = True
    # docs/26 Phase 6 - None for a collector that has never heartbeated with
    # these columns populated. Only apply_ha_policy reads either; owner()/
    # plan()/rebalance() never look at raw health duration, only at
    # `accepting`, which is what keeps the whole live-recompute path from
    # Phase 0 onward oblivious to whichever policy set accepting this way.
    heartbeat_age_s: float | None = None
    healthy_duration_s: float | None = None

    def serves(self, pool_id: str | None, site: str | None) -> bool:
        if self.pool_id is not None:
            return pool_id is not None and pool_id == self.pool_id
        # An endpoint that resolves into a pool belongs to that pool's
        # members and nobody else - "unassigned, never cross-pool" (docs/26
        # Phase 5). An unplaced collector was taking them: in a pool with
        # no healthy member, a serves-everything collector on the IT-OOB
        # network quietly became the owner of BMS devices it has no route
        # to, and the pool page's "owned by nobody" was never true. With
        # zero pools configured no endpoint has a pool_id, so the
        # pre-Phase-5 single-collector estate is untouched.
        if pool_id is not None:
            return False
        if not self.sites:
            return True
        return site is not None and site in self.sites


def _weight(endpoint_id: str, collector_id: str) -> int:
    """Rendezvous weight for one (endpoint, collector) pair.

    sha256 rather than hash(): Python's hash is salted per process, so an
    assignment computed in one API worker would disagree with the next one and
    endpoints would flap between collectors on every request.
    """
    digest = hashlib.sha256(f"{endpoint_id}\x00{collector_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def owner(endpoint_id: str, pool_id: str | None, site: str | None,
          collectors: list[Collector]) -> str | None:
    """Which collector owns this endpoint, or None if nothing can reach it.

    None is a real answer and must not be silently turned into "everyone". An
    endpoint in a site (or pool) no collector serves is unpolled, and the
    honest response is to say so rather than hand it to a collector that
    cannot route to it - persisted as ``reason = 'pool_empty'`` by the
    assigner, docs/26 Phase 5.
    """
    eligible = [c for c in collectors if c.accepting and c.serves(pool_id, site)]
    if not eligible:
        return None
    return max(eligible, key=lambda c: (_weight(endpoint_id, c.collector_id),
                                        c.collector_id)).collector_id


def plan(endpoints: list[dict[str, Any]],
         collectors: list[Collector]) -> dict[str, str | None]:
    """Owner for every endpoint. Pins win over the hash.

    A pin is an operator saying "this one, here" - a device only one collector
    can reach, or one being drained ahead of maintenance - and it must beat the
    algorithm, or the override is not an override. ``pool_id`` on an endpoint
    (docs/26 Phase 5) is optional - absent, an endpoint sheds straight through
    to the pre-Phase-5 site-only placement, unchanged.
    """
    out: dict[str, str | None] = {}
    for e in endpoints:
        pinned = e.get("collector_id")
        if pinned:
            # A pin to a collector nobody has heard of is KEPT, not reassigned.
            # That collector may simply not have started yet, and moving its
            # endpoints elsewhere in the meantime would double-poll every one
            # of them the moment it does.
            out[str(e["id"])] = pinned
            continue
        out[str(e["id"])] = owner(str(e["id"]), e.get("pool_id"), e.get("site"),
                                  collectors)
    return out


def effective(endpoints: list[dict[str, Any]], collectors: list[Collector],
              recorded: dict[str, str | None]) -> dict[str, str | None]:
    """Who actually owns every endpoint: the assigner's record where it is
    still valid, the live plan where it is not (docs/26 Phase 5/6).

    The record is what the assigner decided with damping (an ordinary
    rebalance waits for a real imbalance) and HA policy (failover and
    failback in pools that opted in). Serving the live plan instead - as
    build_assignment did until this - meant neither of those ever reached a
    collector: every HRW opinion change moved endpoints at once, and a
    failover changed the record while the silent collector kept being
    handed the work.

    Precedence, per endpoint:
      1. a pin - the operator's word, immediately, not a tick later;
      2. the recorded owner, if it is in the fleet, accepting, and still
         serves the endpoint's pool or site;
      3. the live plan - for an endpoint the assigner has not recorded yet
         (new, or no assigner has run at all), a record naming nobody
         (pool_empty, which the plan may now be able to fill), and a record
         gone stale between ticks (its owner drained, retired, or moved to
         another pool).

    Health is deliberately NOT a validity test for the record: outside an
    HA pool a silent collector keeps its shard (sharding's founding rule),
    and inside one the assigner has already moved the record away.
    """
    by_id = {c.collector_id: c for c in collectors}
    live = plan(endpoints, collectors) if collectors else {}
    out: dict[str, str | None] = {}
    for e in endpoints:
        eid = str(e["id"])
        pinned = e.get("collector_id")
        if pinned:
            out[eid] = pinned
            continue
        owner_id = recorded.get(eid)
        c = by_id.get(owner_id) if owner_id else None
        if c is not None and c.accepting and c.serves(e.get("pool_id"), e.get("site")):
            out[eid] = owner_id
        else:
            out[eid] = live.get(eid)
    return out


def owned_by(endpoints: list[dict[str, Any]], collectors: list[Collector],
             collector_id: str) -> list[dict[str, Any]]:
    """The subset of endpoints this collector should poll."""
    assignment = plan(endpoints, collectors)
    return [e for e in endpoints if assignment.get(str(e["id"])) == collector_id]


def movement(before: dict[str, str | None],
             after: dict[str, str | None]) -> int:
    """How many endpoints changed hands. The number that matters on a rebalance."""
    return sum(1 for k, v in after.items() if before.get(k) != v)


def distribution(assignment: dict[str, str | None]) -> dict[str, int]:
    out: dict[str, int] = {}
    for owner_id in assignment.values():
        key = owner_id or "(unassigned)"
        out[key] = out.get(key, 0) + 1
    return out


# ----------------------------------------------------------------- damping

# "Rebalance only when a member deviates from the pool mean by >=10
# endpoints and 2x" - docs/26 Phase 5's own wording for the acceptance bar.
# Both numbers are absolute floors as well as ratios on purpose: 2x of a
# 3-endpoint pool is 6, an utterly routine day-to-day fluctuation nobody
# should call an imbalance, and 10 endpoints on a 10,000-endpoint pool is
# 0.1%, noise the ratio alone would still fire on.
REBALANCE_MIN_DELTA = 10
REBALANCE_MIN_RATIO = 2.0


def _mandatory_moves(current: dict[str, str | None],
                     collectors_by_id: dict[str, Collector]) -> set[str]:
    """Endpoint ids whose CURRENT owner can no longer hold them at all - the
    owner is gone from the fleet or `accepting` is False. These bypass
    damping entirely: an operator draining a collector is a decision, not
    routine jitter, and docs/26 Phase 5's own acceptance bar ("a drain
    completes with zero polling gaps beyond one interval") means the very
    next assigner run, not whatever the deviation threshold would otherwise
    hold it to.

    Deliberately NOT `not c.healthy` on its own: a merely stale heartbeat
    for a collector nobody has decided to fail over must not move anything
    by itself, exactly as Phase 0's own docstring on this module states -
    "failover is deliberately not automatic". docs/26 Phase 6's
    `apply_ha_policy` is what turns sustained unhealth into `accepting =
    False`, and only for a pool an operator actually gave real HA to
    (`min_members >= 2`); this function then treats that exactly like a
    drain, which is correct - by the time `accepting` is False here, the
    policy decision has already been made upstream.
    """
    out: set[str] = set()
    for endpoint_id, owner_id in current.items():
        if owner_id is None:
            continue
        c = collectors_by_id.get(owner_id)
        if c is None or not c.accepting:
            out.add(endpoint_id)
    return out


def rebalance(current: dict[str, str | None], target: dict[str, str | None],
              endpoints: list[dict[str, Any]],
              collectors: list[Collector]) -> dict[str, str | None]:
    """The persisted plan the assigner should actually write - docs/26
    Phase 5. ``current`` is what ``endpoint_assignment`` holds today,
    ``target`` is a fresh ``plan()`` computed against the fleet as it
    stands right now. The two disagree constantly - a heartbeat landing a
    second late flips ``healthy`` and back - and adopting ``target``
    wholesale on every tick would mean every one of those blips reassigns
    endpoints and resets their counter baselines.

    Endpoints are grouped by their exact (pool_id, site) pair - the same
    two values ``Collector.serves`` decides eligibility from, so "this
    group's members" means the identical set here and in ``plan()``. A
    group's assignment moves to ``target`` only if at least one of its
    endpoints has a mandatory move (its current owner is gone, unhealthy
    or draining), or the group's own deviation - what ``target`` would
    make each of the group's eligible collectors hold, against the
    group's mean - crosses REBALANCE_MIN_DELTA *and* REBALANCE_MIN_RATIO.
    Otherwise the group is left exactly as ``current`` says, even where
    ``target`` would have quietly preferred a different owner for a
    handful of endpoints - the whole point of damping is that "HRW's
    opinion changed slightly" is not by itself a reason to move anything.

    An endpoint present in ``target`` but missing from ``current`` (new
    since the last run) always takes ``target``'s answer - there is no
    "current" to damp against yet.
    """
    collectors_by_id = {c.collector_id: c for c in collectors}
    mandatory = _mandatory_moves(current, collectors_by_id)

    groups: dict[tuple[str | None, str | None], list[str]] = {}
    for e in endpoints:
        eid = str(e["id"])
        groups.setdefault((e.get("pool_id"), e.get("site")), []).append(eid)

    triggered: set[tuple[str | None, str | None]] = set()
    for key, ids in groups.items():
        pool_id, site = key
        if any(eid in mandatory for eid in ids):
            triggered.add(key)
            continue
        if not any(current.get(eid) != target.get(eid) for eid in ids):
            continue  # target already agrees with current - nothing to damp
        members = [c for c in collectors if c.accepting and c.serves(pool_id, site)]
        if not members:
            continue
        # Deviation is measured on what CURRENT holds today, not on
        # target's own (already near-perfectly balanced) numbers - a
        # target distribution deviating from its OWN mean is nearly
        # always ~0, since that is what "balanced" means. What actually
        # needs damping is a NEW member sitting at 0 while an existing one
        # still holds everything, or the reverse after enough time passes.
        current_counts: dict[str, int] = {}
        for eid in ids:
            owner_id = current.get(eid)
            if owner_id:
                current_counts[owner_id] = current_counts.get(owner_id, 0) + 1
        mean = len(ids) / len(members)
        if mean <= 0:
            continue
        max_count = max((current_counts.get(c.collector_id, 0) for c in members),
                        default=0)
        deviation = max_count - mean
        if deviation >= REBALANCE_MIN_DELTA and max_count >= mean * REBALANCE_MIN_RATIO:
            triggered.add(key)

    out: dict[str, str | None] = {}
    for key, ids in groups.items():
        for eid in ids:
            if eid in mandatory or eid not in current or key in triggered:
                out[eid] = target.get(eid)
            else:
                out[eid] = current.get(eid)
    return out


# ------------------------------------------------------------------- HA

# docs/26 Phase 6's own numbers. STALE_AFTER_S is not enforced here - it is
# what live_collectors's own stale_after_s cutoff already means by
# Collector.healthy, computed before this module ever sees a Collector -
# named here only so the relationship between the three is written down in
# one place: 60s is an ALARM ("this collector needs attention"), 180s is
# FAILOVER ("this pool needs to act"), 600s is how long a recovered member
# waits before FAILBACK trusts it again.
STALE_AFTER_S = 60.0
FAILOVER_AFTER_S = 180.0
FAILBACK_AFTER_S = 600.0


def apply_ha_policy(collectors: list[Collector],
                    pool_min_members: dict[str, int],
                    frozen_pools: frozenset[str] = frozenset()) -> list[Collector]:
    """Overrides ``accepting`` for HA pools only - every non-HA pool
    (``min_members`` under 2, the default for a `collector_pool` row) is
    untouched, which is Phase 0's original, deliberate choice: failover is
    not automatic unless an operator has actually asked for N+1 by giving a
    pool a real ``min_members``.

    ``frozen_pools`` is docs/26 Phase 6's "never during a change-freeze
    blackout" (migration 0084): a pool listed here is exempted from the
    automatic failover/failback logic below entirely, keeping every
    member's `accepting` exactly as given - an unplanned failure during a
    declared change freeze still shows up as `healthy: false` (the alarm at
    STALE_AFTER_S is untouched by this), it just does not trigger the
    platform moving anything on its own while change control asked for
    nothing to move. An explicit drain (`accepting` already False, an
    operator's own decision) is NOT covered by this exemption - see the
    `not c.accepting: continue` line below, which is checked first.

    Within an HA pool, a collector whose heartbeat has been stale past
    FAILOVER_AFTER_S is treated as not accepting - `rebalance`'s existing
    mandatory-move handling (unchanged from docs/26 Phase 5) then moves its
    endpoints to whichever pool member is still accepting, on the very next
    tick, satisfying "a drain completes with zero polling gaps beyond one
    interval" for an unplanned failure the same way it already does for a
    planned drain.

    A collector that has JUST come back (heartbeat fresh again, but
    ``healthy_duration_s`` under FAILBACK_AFTER_S) is ALSO held out of
    ``accepting`` - but only while the pool already has some other member
    genuinely healthy and accepting. That second condition is what tells a
    recovering primary apart from a pool's very first member ever: an empty
    HA pool cannot be made to wait ten minutes before it does anything at
    all, because there is no standby it would be protecting anyone from -
    the quarantine exists to stop a flapping primary from snatching work
    back the instant a heartbeat happens to land, not to slow down a pool
    that has nothing else serving it yet.

    Known, deliberate simplification: this also means a genuinely NEW
    third member joining an already-healthy HA pool sits out for its first
    ten minutes too, indistinguishable here from a recovering one - the
    plan's acceptance bar only asks about restart/failback, and treating
    the two cases identically is far simpler than trying to tell them
    apart from heartbeat history alone.
    """
    by_pool: dict[str, list[Collector]] = {}
    for c in collectors:
        if c.pool_id is not None:
            by_pool.setdefault(c.pool_id, []).append(c)

    out: list[Collector] = []
    for c in collectors:
        if (c.pool_id is None or pool_min_members.get(c.pool_id, 1) < 2
                or not c.accepting or c.pool_id in frozen_pools):
            out.append(c)
            continue

        accepting = c.accepting
        if c.heartbeat_age_s is not None and c.heartbeat_age_s > FAILOVER_AFTER_S:
            accepting = False
        elif (c.healthy_duration_s is not None
              and c.healthy_duration_s < FAILBACK_AFTER_S):
            others_healthy = any(
                o.collector_id != c.collector_id and o.accepting
                and (o.heartbeat_age_s is None or o.heartbeat_age_s <= FAILOVER_AFTER_S)
                for o in by_pool[c.pool_id])
            if others_healthy:
                accepting = False

        out.append(c if accepting == c.accepting else
                   Collector(collector_id=c.collector_id, sites=c.sites,
                             pool_id=c.pool_id, healthy=c.healthy, accepting=accepting,
                             heartbeat_age_s=c.heartbeat_age_s,
                             healthy_duration_s=c.healthy_duration_s))
    return out
