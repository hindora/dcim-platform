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

    def serves(self, pool_id: str | None, site: str | None) -> bool:
        if self.pool_id is not None:
            return pool_id is not None and pool_id == self.pool_id
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
    owner is gone from the fleet, unhealthy, or draining. These bypass
    damping entirely: an operator draining a collector, or a collector
    dying, is a decision or a fact, never routine jitter, and docs/26 Phase
    5's own acceptance bar ("a drain completes with zero polling gaps
    beyond one interval") means the very next assigner run, not whatever a
    30-minute damping window would otherwise hold it to.
    """
    out: set[str] = set()
    for endpoint_id, owner_id in current.items():
        if owner_id is None:
            continue
        c = collectors_by_id.get(owner_id)
        if c is None or not c.healthy or not c.accepting:
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
