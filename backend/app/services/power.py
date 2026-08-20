"""Power chain analytics: what feeds this load, and is it still redundant.

The verdict is the point. During an event nobody wants a topology diagram; they
want to know whether the thing they are about to lose is the last feed.

Three things this is careful about, because each is a way to be confidently
wrong:

* **Dual-corded is not redundant.** Two cords into the same PDU, or two cords
  from two PDUs that are both on the A side, survive nothing. Redundancy needs
  two INDEPENDENT paths, which is what redundancy_side records.

* **A path is only as good as its worst hop.** A feed through an offline UPS is
  not a feed, however healthy the PDU below it looks.

* **Measured and derived load are different claims.** Rack PDUs on this fleet
  report no power at all, so their load is the sum of what they feed. That is
  useful and it is not a measurement, and the response says which it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

# Verdicts, in the words the API spec uses.
N_PLUS_1 = "N+1"
SINGLE_FEED = "single_feed"
NO_FEED = "no_feed"

# IT load and mechanical plant: the things whose loss is an outage. A PDU on
# one feed is expected - a PDUA is an A-side device by definition - so listing
# them would bury the findings that matter.
_CRITICAL_TYPES = frozenset({
    "server", "switch", "router", "firewall", "load_balancer", "storage",
    "crah", "cdu", "chiller", "pump", "cooling_tower",
})

# Gear whose own load is worth reporting fleet-wide.
_SUPPLY_TYPES = frozenset({
    "utility_feed", "generator", "switchgear", "ats", "ups", "mcc", "mpp", "rpp",
})

# A hop in this state breaks the path through it.
DEAD_STATUSES = frozenset({"OFFLINE"})


@dataclass
class Hop:
    device_id: str
    name: str
    device_type: str
    status: str = "UNKNOWN"
    max_severity: str = "CLEAR"
    load_pct: float | None = None
    load_w: float | None = None
    # "measured" when the device reports its own load, "derived" when it is the
    # sum of what it feeds, None when neither is available.
    load_source: str | None = None
    # Other devices that also feed this hop. Named rather than silently
    # dropped: an ATS is fed by both a utility lineup and a generator lineup,
    # and a chain that picks one and shows "fed from the generator" while the
    # site is running on utility is worse than saying both.
    alternate_feeders: list[str] = field(default_factory=list)


@dataclass
class Path:
    side: str | None
    hops: list[Hop] = field(default_factory=list)
    reaches_source: bool = False
    # Every device upstream of this feed, following ALL branches. The displayed
    # hops are one readable line through that set; this is the whole of it, and
    # it is what "shared" has to be computed from.
    upstream_closure: set[str] = field(default_factory=set)

    @property
    def healthy(self) -> bool:
        """Every hop up to the source is alive.

        A single dead hop breaks the path: power does not route around a failed
        UPS the way a packet routes around a failed switch.
        """
        return self.reaches_source and not any(
            h.status in DEAD_STATUSES for h in self.hops)

    @property
    def broken_at(self) -> Hop | None:
        return next((h for h in self.hops if h.status in DEAD_STATUSES), None)


def verdict(paths: list[Path]) -> tuple[str, str]:
    """Redundancy verdict, with the reason spelled out.

    The reason matters as much as the verdict. "single_feed" on a server that
    an operator believes is dual-corded is a finding, and it is only actionable
    if the answer says WHY - both cords on the A side reads very differently
    from one cord in total.
    """
    if not paths:
        return NO_FEED, "nothing in the graph feeds this device"

    live = [p for p in paths if p.healthy]
    if not live:
        broken = next((p.broken_at for p in paths if p.broken_at), None)
        if broken is not None:
            return NO_FEED, (
                f"every feed is broken; the nearest failure is "
                f"{broken.name} ({broken.status})")
        return NO_FEED, "no feed reaches a source"

    sides = {p.side for p in live if p.side in ("A", "B")}
    if len(sides) >= 2:
        return N_PLUS_1, (
            f"fed from {len(live)} live paths across sides "
            f"{', '.join(sorted(sides))}")

    if len(live) > 1 and len(sides) <= 1:
        # The finding worth having: cabled twice, protected once.
        where = next(iter(sides), None)
        return SINGLE_FEED, (
            f"{len(live)} live feeds but all on "
            f"{'side ' + where if where else 'one undetermined path'} - "
            f"cabled twice, protected once")

    only = next(iter(sides), None)
    dead = [p for p in paths if not p.healthy]
    tail = f"; {len(dead)} other feed(s) broken" if dead else ""
    return SINGLE_FEED, (
        f"one live feed{' on side ' + only if only else ''}{tail}")


def shared_hops(paths: list[Path], hop_of: dict[str, Hop] | None = None) -> list[Hop]:
    """Devices that appear in every path: the common-mode failure points.

    A 2N load is only 2N below the point where its paths diverge. The switchgear
    both sides hang off is a single point of failure no amount of dual-cording
    fixes, and an audit that reports "N+1" without naming it is telling half the
    story.
    """
    if len(paths) < 2:
        return []
    # Intersect the full closures, not the displayed chains. Two paths that
    # both reach both switchgear lineups share both, and computing this from
    # the single displayed line would report whichever branch happened to be
    # picked - an artefact, not a fact about the plant.
    sets = [p.upstream_closure or {h.device_id for h in p.hops} for p in paths]
    common = set.intersection(*sets)
    if not common:
        return []

    # Every common device, not just the ones on the displayed line. Filtering
    # by the displayed chain hid SWGR1, GEN1 and the utility feed - all shared
    # by both sides - and under-reporting single points of failure is the wrong
    # direction for an audit to be wrong in.
    lookup = dict(hop_of or {})
    for path in paths:
        for h in path.hops:
            lookup.setdefault(h.device_id, h)

    on_chain = [h for h in paths[0].hops if h.device_id in common]
    named = {h.device_id for h in on_chain}
    rest = sorted((lookup[i] for i in common if i not in named and i in lookup),
                  key=lambda h: h.name)
    return on_chain + rest


def build_paths(device_id: str, feeders: dict[str, list[tuple[str, str | None]]],
                hop_of: dict[str, Hop], sources: set[str],
                max_hops: int = 12) -> list[Path]:
    """Walk upstream from a load, one path per immediate feed.

    ``feeders`` maps a device to (feeder_id, side) pairs. Each immediate feed
    becomes its own path, because that is what an operator unplugs: a cord, not
    an abstract side.

    Where a path branches upstream the first feeder is followed. That is a
    simplification and it is the safe direction: the shared trunk above the
    transfer switches is common to both sides anyway, so which of two identical
    switchgear lineups is listed does not change the verdict.
    """
    paths: list[Path] = []
    for feeder_id, side in feeders.get(device_id, []):
        # The displayed line: one feeder per hop, so it reads as a chain.
        hops: list[Hop] = []
        seen = {device_id}
        cur: str | None = feeder_id
        reached = False
        for _ in range(max_hops):
            if cur is None or cur in seen:
                break
            seen.add(cur)
            hop = hop_of.get(cur)
            if hop is None:
                break
            ups = feeders.get(cur, [])
            hops.append(replace(hop, alternate_feeders=[
                hop_of[f].name for f, _ in ups[1:] if f in hop_of]))
            if cur in sources:
                reached = True
                break
            cur = ups[0][0] if ups else None

        # The whole truth: every device upstream of this feed by any branch.
        closure: set[str] = set()
        stack = [feeder_id]
        while stack:
            node = stack.pop()
            if node in closure or node == device_id:
                continue
            closure.add(node)
            stack.extend(f for f, _ in feeders.get(node, []))

        paths.append(Path(side=side, hops=hops, reaches_source=reached,
                          upstream_closure=closure))
    return paths


def summarise(paths: list[Path]) -> dict[str, Any]:
    kind, reason = verdict(paths)
    return {
        "redundancy": kind,
        "reason": reason,
        "live_paths": sum(1 for p in paths if p.healthy),
        "total_paths": len(paths),
    }


async def chain_for(session, device_id: str) -> dict[str, Any]:
    """Assemble the full chain view for one load."""
    from app.repositories import power as repo

    hops_raw = await repo.hop_states(session)
    if device_id not in hops_raw:
        raise PowerError(f"no device {device_id}")

    edges = await repo.power_edges(session)
    sources = await repo.source_devices(session)
    derived = await repo.derived_load_w(session)

    feeders: dict[str, list[tuple[str, str | None]]] = {}
    for e in edges:
        feeders.setdefault(e["load_dev"], []).append(
            (e["feeder"], e["redundancy_side"]))
    # Deterministic order so the same chain reads the same way twice, and so
    # side A is listed before side B rather than in whatever order the rows
    # arrived.
    for k in feeders:
        feeders[k].sort(key=lambda f: (f[1] or "~", f[0]))

    def to_hop(dev_id: str) -> Hop:
        r = hops_raw[dev_id]
        load_w = r.get("power_w")
        source = "measured" if load_w is not None else None
        if load_w is None and dev_id in derived:
            load_w, source = derived[dev_id], "derived"
        return Hop(
            device_id=dev_id, name=r["name"], device_type=r["device_type"],
            status=r["status"], max_severity=r["max_severity"],
            load_pct=r.get("load_pct"),
            load_w=float(load_w) if load_w is not None else None,
            load_source=source,
        )

    hop_of = {k: to_hop(k) for k in hops_raw}
    paths = build_paths(device_id, feeders, hop_of, sources)

    return {
        "device": hop_of[device_id],
        **summarise(paths),
        "paths": paths,
        "shared_upstream": shared_hops([p for p in paths if p.hops], hop_of),
    }


class PowerError(ValueError):
    """Bad request, with a message meant for the caller."""


async def scope_summary(session) -> dict[str, Any]:
    """Fleet power picture: load per source device, and redundancy census.

    The census is the number worth watching. "How many loads are currently
    running on one feed" is the question a capacity or maintenance conversation
    starts from, and it changes without anyone touching the inventory.
    """
    from app.repositories import power as repo

    hops_raw = await repo.hop_states(session)
    edges = await repo.power_edges(session)
    sources = await repo.source_devices(session)
    derived = await repo.derived_load_w(session)

    feeders: dict[str, list[tuple[str, str | None]]] = {}
    for e in edges:
        feeders.setdefault(e["load_dev"], []).append(
            (e["feeder"], e["redundancy_side"]))
    for k in feeders:
        feeders[k].sort(key=lambda f: (f[1] or "~", f[0]))

    hop_of = {}
    for dev_id, r in hops_raw.items():
        load_w = r.get("power_w")
        src = "measured" if load_w is not None else None
        if load_w is None and dev_id in derived:
            load_w, src = derived[dev_id], "derived"
        hop_of[dev_id] = Hop(
            device_id=dev_id, name=r["name"], device_type=r["device_type"],
            status=r["status"], max_severity=r["max_severity"],
            load_pct=r.get("load_pct"),
            load_w=float(load_w) if load_w is not None else None,
            load_source=src)

    census = {N_PLUS_1: 0, SINGLE_FEED: 0, NO_FEED: 0}
    at_risk: list[dict[str, Any]] = []
    # Only real loads: gear that is fed by something. Sources feed and are not
    # fed, and counting them as "no_feed" would be nonsense.
    for dev_id in feeders:
        kind, reason = verdict(build_paths(dev_id, feeders, hop_of, sources))
        census[kind] = census.get(kind, 0) + 1
        if kind != N_PLUS_1 and hop_of[dev_id].device_type in _CRITICAL_TYPES:
            at_risk.append({"device_id": dev_id, "name": hop_of[dev_id].name,
                            "device_type": hop_of[dev_id].device_type,
                            "redundancy": kind, "reason": reason})

    supplies = sorted(
        (h for h in hop_of.values() if h.device_type in _SUPPLY_TYPES),
        key=lambda h: h.name)

    return {
        "redundancy_census": census,
        "at_risk": sorted(at_risk, key=lambda a: a["name"])[:50],
        "at_risk_total": len(at_risk),
        "supplies": supplies,
        "phase_imbalance": await repo.phase_imbalance(session),
    }
