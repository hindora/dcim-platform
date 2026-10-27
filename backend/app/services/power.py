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
from datetime import UTC, datetime, timedelta
from typing import Any

# Verdicts, in the words the API spec uses.
#
# Two live paths on independent sides is 2N, not N+1. N+1 is one spare unit
# inside a single system (four UPS modules carrying a three-module load); 2N is
# two complete systems each able to carry the whole load, which is what an A
# feed and a B feed are. An operator reads the two very differently: N+1
# survives one module, 2N survives a whole side being switched off for work.
TWO_N = "2N"
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
    "transformer",
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
        return TWO_N, (
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
    fixes, and an audit that reports "2N" without naming it is telling half the
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

# IT load: the things whose loss is an outage and which are built to take two
# cords. A plant load is a different question - see _PLANT_TYPES.
_IT_TYPES = frozenset({
    "server", "switch", "router", "firewall", "load_balancer", "storage",
})

# Mechanical plant is single-corded by design: a chiller is fed from one MCC
# through one breaker, and its redundancy is the N+1 spare machine standing
# beside it, not a second cord. Counting a chiller as "a load without full
# redundancy" put fifty plant machines on a list meant for servers and buried
# the three that mattered. So plant is judged in GROUPS - the chillers of a
# site, the CRAHs of a hall - on how many members still have a live feed.
_PLANT_TYPES = frozenset({"chiller", "pump", "cooling_tower", "crah", "cdu"})

#: Group verdicts, worst first.
GROUP_SHORT, GROUP_TIGHT, GROUP_OK = "short", "tight", "ok"

#: A power panel reads through the meter clamped on it; a reading older than
#: this is a stale meter, not a live panel.
METERED_FRESH = timedelta(minutes=15)

#: Load against rating on a supply device, in the bands the estate page uses.
SUPPLY_LOAD_WARN_PCT = 80.0
SUPPLY_LOAD_CRITICAL_PCT = 90.0


def plant_group_verdict(n: int, fed: int) -> tuple[str, str]:
    """How a plant group stands on feeds: every member fed is OK (the N+1
    spare is intact); one lost is tight (the spare is spent); more is short."""
    if fed >= n:
        return GROUP_OK, f"all {n} fed"
    if fed == n - 1:
        return GROUP_TIGHT, f"{fed} of {n} fed - the N+1 spare is spent"
    return GROUP_SHORT, f"{fed} of {n} fed"


def census(loads: dict[str, dict[str, Any]],
           verdicts: dict[str, tuple[str, str]]) -> dict[str, Any]:
    """Split the per-load verdicts into the IT census and the plant groups.

    `loads` is {device_id: {device_type, name, room_id, room_name, site_code}}
    for every fed device in scope; `verdicts` is {device_id: (kind, reason)}
    from `verdict()`. IT loads are counted by kind; plant loads are grouped
    by (room, type) and judged on how many members have at least one live
    feed.
    """
    it = {TWO_N: 0, SINGLE_FEED: 0, NO_FEED: 0}
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for dev_id, (kind, _reason) in verdicts.items():
        d = loads.get(dev_id)
        if d is None:
            continue
        t = d["device_type"]
        if t in _IT_TYPES:
            it[kind] = it.get(kind, 0) + 1
        elif t in _PLANT_TYPES:
            g = groups.setdefault((d.get("room_id") or "", t), {
                "room_id": d.get("room_id"), "room_name": d.get("room_name"),
                "site_code": d.get("site_code"), "device_type": t,
                "n": 0, "fed": 0, "unfed": []})
            g["n"] += 1
            if kind == NO_FEED:
                g["unfed"].append(d["name"])
            else:
                g["fed"] += 1
    out_groups = []
    for g in groups.values():
        v, why = plant_group_verdict(g["n"], g["fed"])
        out_groups.append({**g, "verdict": v, "why": why,
                           "unfed": sorted(g["unfed"])[:5]})
    out_groups.sort(key=lambda g: ((GROUP_SHORT, GROUP_TIGHT, GROUP_OK).index(g["verdict"]),
                                   g["site_code"] or "", g["room_name"] or "",
                                   g["device_type"]))
    return {
        "it_loads": it,
        "plant_groups": out_groups,
        "plant_groups_short": sum(1 for g in out_groups if g["verdict"] != GROUP_OK),
    }


def supply_verdict(row: dict[str, Any]) -> tuple[str, str | None]:
    """The plant service's verdict, with the chain's two findings on top.

    The plant readers already say on battery, on bypass, de-energised, low
    fuel and the rest. What they cannot say is that a panel with no protocol
    is fine because its meter says so, or that a board the chain reports at
    92 % of rating is one server from a breaker. Both are decided here.
    """
    v, why = row.get("verdict") or "unknown", row.get("why")
    if row.get("device_type") == "transformer" and v == "standby":
        # Its only binary is the cooling fans, and ONAN - fans off, oil
        # convecting - is how a transformer spends most of its life while
        # carrying everything the site draws. "Standby" is the generic word
        # for a machine that is off, and this one is not.
        v, why = "ok", "natural cooling; the fans start when the oil asks"
    if row.get("load_source") == "metered_by":
        if row.get("metered_stale"):
            return "silent", (f"read through {row['metered_by']}, whose last "
                              f"reading is older than "
                              f"{int(METERED_FRESH.total_seconds() // 60)} minutes")
        if v in ("unmonitored", "silent", "unknown"):
            v, why = "ok", f"read through {row['metered_by']}"
    pct = row.get("load_pct")
    if v in ("ok", "standby", "unmonitored", "unknown", "silent", "n_plus_1") \
            and pct is not None:
        if pct >= SUPPLY_LOAD_CRITICAL_PCT:
            return "load_high", (f"{pct:.0f} % of rating - the next load lands "
                                 f"on a breaker")
        if pct >= SUPPLY_LOAD_WARN_PCT:
            return "load_high", (f"{pct:.0f} % of rating - no room for the other "
                                 f"side's load on a failover")
    return v, why


async def scope_summary(session, *, datacenter_id: str | None = None,
                        room_id: str | None = None) -> dict[str, Any]:
    """Fleet power picture in one scope: load per supply device, the
    redundancy census, and the plant groups.

    The census is the number worth watching. "How many loads are running on
    one feed" is the question a capacity or maintenance conversation starts
    from, and it changes without anyone touching the inventory. It is two
    numbers, not one: IT loads by cords, and plant groups by members fed,
    because a chiller on one cord is the design and a server on one cord is
    the finding.
    """
    from app.repositories import panel_metering as meter_repo
    from app.repositories import power as repo
    from app.services import plant as plant_service

    hops_raw = await repo.hop_states(session)
    edges = await repo.power_edges(session)
    sources = await repo.source_devices(session)
    derived = await repo.derived_load_w(session)

    def in_scope(r: dict[str, Any]) -> bool:
        if room_id is not None:
            return r.get("room_id") == room_id
        if datacenter_id is not None:
            return r.get("datacenter_id") == datacenter_id
        return True

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

    # Only real loads, in scope: gear that is fed by something. Sources feed
    # and are not fed, and counting them as "no_feed" would be nonsense.
    verdicts: dict[str, tuple[str, str]] = {}
    loads: dict[str, dict[str, Any]] = {}
    for dev_id in feeders:
        r = hops_raw.get(dev_id)
        if r is None or not in_scope(r):
            continue
        verdicts[dev_id] = verdict(build_paths(dev_id, feeders, hop_of, sources))
        loads[dev_id] = {"device_type": r["device_type"], "name": r["name"],
                         "room_id": r.get("room_id"), "room_name": r.get("room_name"),
                         "site_code": r.get("site_code")}
    cen = census(loads, verdicts)

    # What is at risk: IT on one feed or none, and plant on none. A chiller on
    # one feed is the design; a chiller on no feed is a machine that is off.
    at_risk: list[dict[str, Any]] = []
    for dev_id, (kind, reason) in verdicts.items():
        t = loads[dev_id]["device_type"]
        if (t in _IT_TYPES and kind != TWO_N) or (t in _PLANT_TYPES and kind == NO_FEED):
            at_risk.append({"device_id": dev_id, "name": loads[dev_id]["name"],
                            "device_type": t, "room_name": loads[dev_id]["room_name"],
                            "site_code": loads[dev_id]["site_code"],
                            "redundancy": kind, "reason": reason})
    at_risk.sort(key=lambda a: ((NO_FEED, SINGLE_FEED, TWO_N).index(a["redundancy"]),
                                a["name"]))

    # The supply devices, read by the plant service's readers and verdicts,
    # with the chain's load figures laid on top.
    supplies: list[dict[str, Any]] = []
    plant_rows = {m["device_id"]: m for m in await plant_service.supply(session)}
    rpp_ids = [i for i, m in plant_rows.items() if m["device_type"] == "rpp"]
    meters = await meter_repo.meters_fed_from(session, rpp_ids) if rpp_ids else {}
    now = datetime.now(UTC)
    for dev_id, m in plant_rows.items():
        r = hops_raw.get(dev_id, {})
        if not in_scope({**r, "room_id": m.get("room_id"),
                         "datacenter_id": m.get("site_id")}):
            continue
        hop = hop_of.get(dev_id)
        load_w = hop.load_w if hop else None
        source = hop.load_source if hop else None
        metered_by = None
        metered_stale = False
        links = meters.get(dev_id, [])
        if links and m["device_type"] == "rpp":
            # A power panel has no protocol of its own; the meter clamped
            # on it is its reading, and says whether that is current.
            link = links[0]
            metered_by = link["meter_name"]
            seen = link.get("last_seen")
            metered_stale = (link.get("status") != "ONLINE" or seen is None
                             or (now - seen) > METERED_FRESH)
            if link.get("power_w") is not None and not metered_stale:
                load_w, source = float(link["power_w"]), "metered_by"
            elif load_w is None:
                source = "metered_by"
        rated = m.get("rated_kw")
        load_kw = None if load_w is None else round(load_w / 1000.0, 1)
        pct = (round(100.0 * load_kw / rated, 1)
               if load_kw is not None and rated else
               (round(float(hop.load_pct), 1) if hop and hop.load_pct is not None else None))
        row = {
            # Both: `device_id` is what the platform calls it, `id` is what the
            # table component keys rows on.
            "id": dev_id,
            "device_id": dev_id, "name": m["name"], "device_type": m["device_type"],
            "status": m.get("status") or (hop.status if hop else "UNKNOWN"),
            "max_severity": hop.max_severity if hop else "CLEAR",
            "room_id": m.get("room_id"), "room_name": m.get("room_name"),
            "site_id": m.get("site_id"), "site_code": m.get("site_code"),
            "site_name": m.get("site_name"), "model": m.get("model"),
            "state_label": m.get("state_label"), "states": m.get("states", {}),
            "rated_kw": rated,
            "load_kw": load_kw, "load_pct": pct, "load_source": source,
            "metered_by": metered_by, "metered_stale": metered_stale,
            "carried_kw": m.get("carried_kw"), "voltage_v": m.get("voltage_v"),
            "power_factor": m.get("power_factor"),
            "imbalance_pct": m.get("imbalance_pct"), "thd_pct": m.get("thd_pct"),
            "battery_minutes": m.get("battery_minutes"),
            "battery_health_pct": m.get("battery_health_pct"),
            "battery_c": m.get("battery_c"),
            "fuel_pct": m.get("fuel_pct"), "run_minutes": m.get("run_minutes"),
            "coolant_c": m.get("coolant_c"), "transfers": m.get("transfers"),
            "oil_c": m.get("oil_c"), "winding_c": m.get("winding_c"),
            "alarms_open": m.get("alarms_open", 0),
            "monitored": m.get("monitored", False),
            "verdict": m.get("verdict"), "why": m.get("why"),
        }
        row["verdict"], row["why"] = supply_verdict(row)
        supplies.append(row)
    order = {t: i for i, t in enumerate(("utility_feed", "transformer", "generator",
                                          "switchgear", "ats", "ups", "mcc", "mpp",
                                          "rpp"))}
    supplies.sort(key=lambda s: (s["site_code"] or "", order.get(s["device_type"], 99),
                                 s["name"]))

    # The headline figures the tab's band shows.
    utility = [s for s in supplies if s["device_type"] == "utility_feed"]
    gens = [s for s in supplies if s["device_type"] == "generator"]
    ats = [s for s in supplies if s["device_type"] == "ats"]
    ups = [s for s in supplies if s["device_type"] == "ups"]
    imb = [s for s in supplies if s.get("imbalance_pct") is not None]
    worst_imb = max(imb, key=lambda s: s["imbalance_pct"]) if imb else None
    runtimes = [s["battery_minutes"] for s in ups if s.get("battery_minutes") is not None]
    ups_loads = [s["load_pct"] for s in ups if s.get("load_pct") is not None]
    headline = {
        "utility": {"healthy": sum(1 for s in utility if s["verdict"] != "utility_lost"
                                   and s["status"] == "ONLINE"),
                    "total": len(utility)},
        "generators": {"running": sum(1 for s in gens if s["verdict"] == "engine_running"
                                      or s.get("state_label") == "Running"),
                       "total": len(gens),
                       "fuel_min_pct": min((s["fuel_pct"] for s in gens
                                            if s.get("fuel_pct") is not None), default=None)},
        "ats_on_emergency": sum(1 for s in ats if s["verdict"] == "on_emergency"),
        "ups": {"total": len(ups),
                "on_battery": sum(1 for s in ups if s["verdict"] == "on_battery"),
                "on_bypass": sum(1 for s in ups if s["verdict"] == "on_bypass"),
                "load_max_pct": max(ups_loads, default=None),
                "runtime_min_minutes": min(runtimes, default=None)},
        "on_one_leg": cen["it_loads"][SINGLE_FEED] + cen["it_loads"][NO_FEED],
        "imbalance_worst": (None if worst_imb is None else
                            {"name": worst_imb["name"], "pct": worst_imb["imbalance_pct"]}),
    }

    sites = sorted({(r["datacenter_id"], r.get("site_code")) for r in hops_raw.values()
                    if r.get("datacenter_id")}, key=lambda x: x[1] or "")
    return {
        "scope": {"datacenter_id": datacenter_id, "room_id": room_id},
        "sites": [{"id": i, "code": c} for i, c in sites],
        "redundancy_census": cen["it_loads"],
        "plant_groups": cen["plant_groups"],
        "plant_groups_short": cen["plant_groups_short"],
        "at_risk": at_risk[:50],
        "at_risk_total": len(at_risk),
        "headline": headline,
        "supplies": supplies,
        "phase_imbalance": [{"device_id": s["device_id"], "name": s["name"],
                             "device_type": s["device_type"],
                             "imbalance_pct": s["imbalance_pct"]} for s in imb],
    }
