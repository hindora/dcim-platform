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
site first and balance within it. A collector declares the sites it serves; one
that declares none is treated as serving all, which is the correct default for
a single-site deployment and the reason the existing single collector keeps
working unchanged.

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

    ``sites`` is the set of datacenter codes it can reach. Empty means "any" -
    the single-collector default, and the reason adding this module changes
    nothing for an existing deployment.
    """

    collector_id: str
    sites: frozenset[str] = field(default_factory=frozenset)
    healthy: bool = True

    def serves(self, site: str | None) -> bool:
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


def owner(endpoint_id: str, site: str | None,
          collectors: list[Collector]) -> str | None:
    """Which collector owns this endpoint, or None if nothing can reach it.

    None is a real answer and must not be silently turned into "everyone". An
    endpoint in a site no collector serves is unpolled, and the honest response
    is to say so rather than hand it to a collector that cannot route to it.
    """
    eligible = [c for c in collectors if c.serves(site)]
    if not eligible:
        return None
    return max(eligible, key=lambda c: (_weight(endpoint_id, c.collector_id),
                                        c.collector_id)).collector_id


def plan(endpoints: list[dict[str, Any]],
         collectors: list[Collector]) -> dict[str, str | None]:
    """Owner for every endpoint. Pins win over the hash.

    A pin is an operator saying "this one, here" - a device only one collector
    can reach, or one being drained ahead of maintenance - and it must beat the
    algorithm, or the override is not an override.
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
        out[str(e["id"])] = owner(str(e["id"]), e.get("site"), collectors)
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
