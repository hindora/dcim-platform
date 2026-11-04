"""Cooling-chain correlation: one lost unit, one incident (docs/28 L5, docs/33 Phase 2).

`correlation.py` folds "cannot see it" alarms under a dead switch or feeder.
It deliberately never touches a temperature: a hot rack behind a dead switch
is still hot. This module handles the other cascade, where the rack is hot
BECAUSE something upstream stopped cooling it.

    chiller / tower / CHW pump        plant
      -> CRAH or CDU                  unit
        -> rack probe, room sensor    zone
          -> server, switch           IT

with the electrical feeder of a cooling machine above the machine (an MCC trip
stops a chiller; an MPP trip stops a CRAH). The physics of the chain runs that
way with lags of minutes: plant first, then CRAH supply, then rack inlet, then
server fans and CPUs (docs/28 "one chiller trip").

HOW THE GRAPH IS KNOWN, AND HOW WELL
- Plant to unit: not drawn. One chilled-water header per site serves every
  CRAH and CDU in it, which is how the modelled sites are built and how most
  single-plant enterprise sites are built. A multi-plant campus would need the
  CHW loop drawn; the `cooling` layer has no loop entity yet.
- Unit to rack, air side: by room. Every CRAH in a hall is taken to serve every
  rack in it. That is the room-level fallback docs/28 plans until the fitted
  rack-to-CRAH influence matrix exists (Phase 4). It says "Hall A cooling",
  not "CRAH-03's zone", and the evidence says so.
- CDU to server: drawn (cooling layer, cold-plate loops).
- Feeder to machine: drawn (power layer).

WHAT IS FOLDED
A symptom is folded (`is_symptom`) under a root only when the root is no less
severe. A consequence MORE severe than its cause - a CPU at its critical limit
under a MAJOR CRAH alarm - asks for a different action (shed load, not fix the
CRAH) and stays visible. It is still tied to the incident as a member, so the
console reads one story; it just keeps its own notification.

WHAT IS NOT A ROOT
- A maintenance-shelved alarm: planned work explains itself.
- A cooling machine in a room with an open fire alarm: the fire panel's
  interlock stops CRAHs on purpose (pre-discharge), and calling that a cooling
  fault would point the operator at the wrong system during a fire.
- Conditions that do not take cooling away: a dirty filter, a hunting valve,
  low delta-T, condensation risk. They are efficiency and maintenance faults,
  and an hours-old one would otherwise swallow every unrelated hot rack.

TIME
A root may start up to `LEAD_S` after its symptom (a rack probe can trip
before the CRAH's own alarm finishes its dwell) and up to `LAG_S` before it.
Past an hour the link is too weak to assert: a CRAH that has been down all
morning with the room holding fine is not why one rack went hot at lunch.

RELEASE
A cleared cooling root does not release its symptoms at once. Air takes
minutes to recover (stopped-CRAH discharge soaks with a time constant of
about four minutes), so a rack probe still reads high for a while after the
unit restarts. Releasing then would turn one restoration into a burst of
fresh roots. They are held for `RESTORE_HOLD_S` and released by the sweep if
still open.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.integrations.config import SEVERITY_RANK

log = get_logger("alarms.cooling_correlation")

# --- tiers ------------------------------------------------------------------

TIER_RANK = {"server": 0, "zone": 1, "unit": 2, "plant": 3, "power": 4}

_PLANT = frozenset({"chiller", "cooling_tower", "pump", "valve"})
_UNIT = frozenset({"crah", "crac", "cdu", "in_row_cooler", "rdhx"})
#: Units that cool the room's air. A CDU is a unit too, but it cools the
#: cold plates on its own loop and nothing else, so it reaches a server only
#: through a drawn cooling edge, never by sharing a room.
_AIR_UNITS = frozenset({"crah", "crac", "in_row_cooler", "rdhx"})
_ZONE = frozenset({"sensor", "pdu"})
_IT = frozenset({"server", "switch", "oob_switch", "firewall", "router",
                 "load_balancer", "storage"})


def device_tier(device_type: str) -> str | None:
    if device_type in _PLANT:
        return "plant"
    if device_type in _UNIT:
        return "unit"
    if device_type in _ZONE:
        return "zone"
    if device_type in _IT:
        return "server"
    return None


# --- which alarms take part ---------------------------------------------------

#: Cooling conditions that do not remove cooling. See the module docstring.
NOT_A_CAPACITY_LOSS = frozenset({
    "crah_filter_dirty", "cooling_filter_dirty", "crah_low_delta_t",
    "chiller_low_delta_t", "crah_valve_hunting", "valve_hunting",
    "chiller_short_cycling", "cdu_condensation_risk", "tower_basin_imbalance",
    "makeup_water_low", "humidity_high", "humidity_low", "humidity_critical",
})

#: Generic unit alarms that arrive without the cooling category: a CRAH's
#: summary alarm point, and "unit reports stopped".
_UNIT_GENERIC = frozenset({"equipment_alarm", "plant_unit_stopped", "supply_temp_high"})

#: A whole-machine electrical trip on the feeder of a cooling machine.
POWER_ROOT_TYPES = frozenset({"breaker_tripped", "switchgear_breaker_trip"})

#: What heat looks like downstream: a rack probe, a room sensor, a CPU.
THERMAL_SYMPTOMS = frozenset({
    "ambient_temp_high", "ambient_temp_critical",
    "sensor_ambient_temp_high", "sensor_ambient_temp_critical",
    "sensor_mid_temp_high", "sensor_outlet_temp_high", "pdu_temp_high",
    "inlet_rate_of_change", "cpu_temp_high", "cpu_temp_critical",
    # a server's own inlet sensor (Redfish/IPMI), the densest signal there is
    "inlet_temp_high", "inlet_temp_critical",
})

#: The same, by what was measured: an operator's own rule on these metrics
#: is a heat alarm whatever it was named.
THERMAL_METRICS = frozenset({"inlet_temperature", "ambient_temperature",
                             "cpu_temperature", "exhaust_temperature"})


def is_thermal(alarm_type: str, metric_key: str | None = None) -> bool:
    return alarm_type in THERMAL_SYMPTOMS or (metric_key or "") in THERMAL_METRICS


_COOLING_PREFIXES = ("crah_", "crac_", "chiller_", "cdu_", "cooling_", "tower_",
                     "pump_", "valve_", "plant_")


def _is_cooling_condition(alarm_type: str, category: str | None) -> bool:
    if alarm_type in NOT_A_CAPACITY_LOSS:
        return False
    return (category == "cooling" or alarm_type in _UNIT_GENERIC
            or alarm_type.startswith(_COOLING_PREFIXES))


def root_eligible(alarm_type: str, category: str | None, tier: str,
                  instance: str = "") -> bool:
    """Can this alarm, on a device acting at `tier`, explain heat below it?"""
    if tier == "power":
        return alarm_type in POWER_ROOT_TYPES and not instance
    if tier in ("plant", "unit"):
        return _is_cooling_condition(alarm_type, category)
    return False


def symptom_eligible(alarm_type: str, category: str | None, tier: str | None,
                     metric_key: str | None = None) -> bool:
    """Can this alarm be the consequence of something upstream?"""
    if tier in ("server", "zone"):
        return is_thermal(alarm_type, metric_key)
    if tier in ("unit", "plant"):
        # A CRAH discharging warm because the chiller tripped; a chiller
        # stopped because its MCC tripped.
        return _is_cooling_condition(alarm_type, category)
    return False


def might_take_part(alarm_type: str, category: str | None,
                    metric_key: str | None = None) -> bool:
    """A check on the name alone, so the raise path pays nothing for the rest.

    A trap-raised alarm arrives without its category (the device's role
    decides it later), hence the name prefixes.
    """
    return is_thermal(alarm_type, metric_key) or _is_cooling_condition(alarm_type, category)


LEAD_S = 600          # a root may start this long after its symptom
LAG_S = 3600          # ...and no more than this long before it
RESTORE_HOLD_S = 600  # symptoms held after the root clears


# --- the graph ----------------------------------------------------------------

@dataclass
class Node:
    id: str
    name: str
    device_type: str
    room_id: str | None
    datacenter_id: str | None

    @property
    def tier(self) -> str | None:
        return device_tier(self.device_type)


@dataclass
class CoolingGraph:
    nodes: dict[str, Node]
    #: cooling-layer neighbours in either direction (the layer's loops are
    #: drawn both ways, so direction is taken from the tier, not the edge)
    cooling_nbrs: dict[str, set[str]] = field(default_factory=dict)
    #: cooling-layer upstream (a side) - used only between plant machines
    cooling_up: dict[str, set[str]] = field(default_factory=dict)
    #: power-layer feeders of each load
    feeders: dict[str, set[str]] = field(default_factory=dict)
    room_units: dict[str, list[str]] = field(default_factory=dict)
    dc_plant: dict[str, list[str]] = field(default_factory=dict)
    _memo: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def build(cls, devices: list[dict[str, Any]], cooling: list[tuple[str, str]],
              power: list[tuple[str, str]]) -> CoolingGraph:
        nodes = {d["id"]: Node(d["id"], d["name"], d["device_type"], d.get("room_id"),
                               d.get("datacenter_id")) for d in devices}
        g = cls(nodes)
        for a, b in cooling:
            if a in nodes and b in nodes:
                g.cooling_nbrs.setdefault(a, set()).add(b)
                g.cooling_nbrs.setdefault(b, set()).add(a)
                g.cooling_up.setdefault(b, set()).add(a)
        for a, b in power:
            if a in nodes and b in nodes:
                g.feeders.setdefault(b, set()).add(a)
        for n in nodes.values():
            if n.device_type in _AIR_UNITS and n.room_id:
                g.room_units.setdefault(n.room_id, []).append(n.id)
            if n.tier == "plant" and n.datacenter_id:
                g.dc_plant.setdefault(n.datacenter_id, []).append(n.id)
        return g

    def ancestors(self, device_id: str) -> dict[str, str]:
        """Every device upstream on the cooling chain -> the tier it acts at."""
        hit = self._memo.get(device_id)
        if hit is not None:
            return hit
        out: dict[str, str] = {}
        n = self.nodes.get(device_id)
        tier = n.tier if n else None
        if n is None or tier is None:
            self._memo[device_id] = out
            return out

        def add(dev: str, at: str) -> None:
            if dev != device_id and TIER_RANK[at] > TIER_RANK.get(out.get(dev, "server"), -1):
                out[dev] = at

        def unit_up(unit: str) -> None:
            u = self.nodes[unit]
            for p in self.dc_plant.get(u.datacenter_id or "", ()):
                add(p, "plant")
            for f in self.feeders.get(unit, ()):
                add(f, "power")

        def plant_up(plant: str) -> None:
            for f in self.feeders.get(plant, ()):
                add(f, "power")

        if tier in ("server", "zone"):
            explicit = [m for m in self.cooling_nbrs.get(device_id, ())
                        if self.nodes[m].tier in ("unit", "plant")]
            units = list(explicit)
            # Air: every unit in the room, unless this is a unit's own
            # instrument (a CRAH's supply probe, a chiller's CHW sensor).
            if tier == "server" or not explicit:
                units += self.room_units.get(n.room_id or "", [])
            for u in units:
                at = self.nodes[u].tier or "unit"
                add(u, at)
                if at == "unit":
                    unit_up(u)
                else:
                    plant_up(u)
            if not units:
                # A room with no modelled cooling of its own is cooled by the
                # site's plant through something not drawn.
                for p in self.dc_plant.get(n.datacenter_id or "", ()):
                    add(p, "plant")
        elif tier == "unit":
            unit_up(device_id)
        elif tier == "plant":
            plant_up(device_id)
            # Within the plant, only what feeds this machine directly: a
            # tower ahead of its chiller. The loop is drawn round in a circle,
            # so walking further would make every machine its own ancestor.
            for m in self.cooling_up.get(device_id, ()):
                if self.nodes[m].tier == "plant":
                    add(m, "plant")
                    plant_up(m)
        self._memo[device_id] = out
        return out


# --- choosing a root ------------------------------------------------------------

def _ts(v: Any) -> float:
    return v.timestamp() if isinstance(v, datetime) else float(v)


def pick_root(graph: CoolingGraph, symptom: dict[str, Any],
              open_alarms: list[dict[str, Any]],
              fire_rooms: frozenset[str] = frozenset(),
              representatives: dict[tuple[str, str], str] | None = None,
              ) -> dict[str, Any] | None:
    """The alarm upstream of `symptom` that best explains it, or None.

    `open_alarms` are visible open alarms (not symptoms, not shelved). The
    highest tier wins - a chiller trip explains a CRAH's warm discharge, which
    explains the rack - then the earliest onset, then the id for a stable tie.
    `representatives` maps (root id, alarm type) to the one visible alarm
    already speaking for that kind of consequence. The returned dict gains
    `tier`, `suppress` and `representative`.
    """
    sym_dev = symptom["device_id"]
    node = graph.nodes.get(sym_dev)
    if node is None or not symptom_eligible(symptom["alarm_type"], symptom.get("category"),
                                            node.tier, symptom.get("metric_key")):
        return None
    anc = graph.ancestors(sym_dev)
    if not anc:
        return None
    t_sym = _ts(symptom["first_seen"])
    own_rank = TIER_RANK[node.tier or "server"]
    best: tuple | None = None
    for a in open_alarms:
        at = anc.get(a["device_id"])
        if at is None or a["id"] == symptom["id"]:
            continue
        # Same tier is allowed only inside the plant, along a drawn edge (a
        # tower ahead of its chiller). Anywhere else peers are not causes.
        if TIER_RANK[at] < own_rank or (TIER_RANK[at] == own_rank and at != "plant"):
            continue
        if not root_eligible(a["alarm_type"], a.get("category"), at, a.get("instance") or ""):
            continue
        root_node = graph.nodes.get(a["device_id"])
        if root_node and at in ("unit", "plant") and root_node.room_id in fire_rooms:
            continue
        t_root = _ts(a["first_seen"])
        if not (t_sym - LAG_S <= t_root <= t_sym + LEAD_S):
            continue
        key = (-TIER_RANK[at], t_root, a["id"])
        if best is None or key < best[0]:
            best = (key, a, at)
    if best is None:
        return None
    _, root, at = best
    worse = (SEVERITY_RANK.get(symptom["severity"], 9)
             < SEVERITY_RANK.get(root["severity"], 9))
    # A consequence worse than its cause stays visible - ONE of each kind.
    # Ninety servers at critical inlet under one stopped hall are one
    # condition asking for one action (restore the cooling, shed the hall's
    # load): the first says it and the rest fold under the cause. hall-crahs
    # left 90 critical lines on the console before this.
    rep = (representatives or {}).get((root["id"], symptom["alarm_type"]))
    suppress = not worse or (rep is not None and rep != symptom["id"])
    return {**root, "tier": at, "suppress": suppress,
            "representative": worse and not suppress,
            "device_name": graph.nodes[root["device_id"]].name}


# --- database -----------------------------------------------------------------

_GRAPH: tuple[float, CoolingGraph] | None = None
GRAPH_TTL_S = 300.0


def reset_graph() -> None:
    global _GRAPH
    _GRAPH = None


async def load_graph(session: AsyncSession) -> CoolingGraph:
    """The cooling graph, rebuilt at most every five minutes per process.

    A topology upload changes it; five minutes late is fine for a grouping
    decision, and rebuilding per alarm would cost three queries on the
    ingest path.
    """
    global _GRAPH
    now = time.monotonic()
    if _GRAPH is not None and now - _GRAPH[0] < GRAPH_TTL_S:
        return _GRAPH[1]
    devices = (await session.execute(text("""
        SELECT d.id::text AS id, d.name, d.device_type::text AS device_type,
               d.room_id::text AS room_id, r.datacenter_id::text AS datacenter_id
          FROM device d LEFT JOIN room r ON r.id = d.room_id
         WHERE d.decommissioned_at IS NULL
    """))).mappings().all()
    edges = (await session.execute(text("""
        SELECT layer::text AS layer, a_device_id::text AS a, b_device_id::text AS b
          FROM connection
         WHERE layer IN ('cooling', 'power')
    """))).mappings().all()
    g = CoolingGraph.build([dict(d) for d in devices],
                           [(e["a"], e["b"]) for e in edges if e["layer"] == "cooling"],
                           [(e["a"], e["b"]) for e in edges if e["layer"] == "power"])
    _GRAPH = (now, g)
    return g


_OPEN_VISIBLE = """
    SELECT a.id::text AS id, a.device_id::text AS device_id, a.alarm_type,
           a.instance, a.category, a.metric_key, a.severity::text AS severity, a.first_seen
      FROM alarm a
     WHERE a.state <> 'CLEARED' AND NOT a.is_symptom
       AND a.shelved_by_window IS NULL
"""

#: The visible alarm already speaking for each kind of consequence of a root.
_REPRESENTATIVES = """
    SELECT attributes->'l5'->>'root' AS root, alarm_type, id::text AS id, first_seen
      FROM alarm
     WHERE state <> 'CLEARED' AND NOT is_symptom AND attributes ? 'l5'
     ORDER BY first_seen
"""


async def representatives(session: AsyncSession) -> dict[tuple[str, str], str]:
    rows = (await session.execute(text(_REPRESENTATIVES))).mappings().all()
    out: dict[tuple[str, str], str] = {}
    for r in rows:
        out.setdefault((r["root"], r["alarm_type"]), r["id"])
    return out


async def fire_rooms(session: AsyncSession) -> frozenset[str]:
    rows = (await session.execute(text("""
        SELECT DISTINCT d.room_id::text
          FROM alarm a JOIN device d ON d.id = a.device_id
         WHERE a.state <> 'CLEARED' AND a.category = 'fire' AND d.room_id IS NOT NULL
    """))).scalars().all()
    return frozenset(rows)


async def mark(session: AsyncSession, *, alarm_id: str, root: dict[str, Any],
               now: datetime | None = None) -> None:
    """Tie an alarm to its cooling root; fold it only when `suppress`."""
    await session.execute(text("""
        UPDATE alarm
           SET is_symptom = is_symptom OR CAST(:sup AS boolean),
               root_cause_alarm_id = CASE WHEN CAST(:sup AS boolean)
                                          THEN CAST(:root AS uuid)
                                          ELSE root_cause_alarm_id END,
               attributes = coalesce(attributes, '{}'::jsonb) || jsonb_build_object(
                   'l5', jsonb_build_object('root', CAST(:root AS text),
                                            'tier', CAST(:tier AS text),
                                            'suppressed', CAST(:sup AS boolean),
                                            'at', CAST(:at AS text)))
         WHERE id = CAST(:id AS uuid)
    """), {"id": alarm_id, "root": root["id"], "tier": root["tier"],
          "sup": bool(root["suppress"]),
          "at": (now or datetime.now(UTC)).isoformat()})


async def correlate(session: AsyncSession, *, alarm: dict[str, Any], device_id: str,
                    alarm_type: str) -> dict[str, Any] | None:
    """On the raise path: is this new alarm the consequence of a cooling root?

    Cheap for everything that is not a cooling or thermal alarm: the type is
    checked against two small sets before anything touches the database.
    Returns the root (with `layer`, `device_name`, `suppress`) or None.
    """
    metric_key = alarm.get("metric_key")
    if not might_take_part(alarm_type, alarm.get("category"), metric_key):
        return None
    graph = await load_graph(session)
    node = graph.nodes.get(device_id)
    if node is None or node.tier is None:
        return None
    if node.tier in ("server", "zone") and not is_thermal(alarm_type, metric_key):
        return None
    category = alarm.get("category")
    if category is None and node.tier in ("unit", "plant"):
        category = await session.scalar(text(
            "SELECT category FROM alarm WHERE id = CAST(:id AS uuid)"), {"id": alarm["id"]})
    if not symptom_eligible(alarm_type, category, node.tier, metric_key):
        return None
    anc = graph.ancestors(device_id)
    if not anc:
        return None
    rows = (await session.execute(text(_OPEN_VISIBLE + """
       AND a.device_id = ANY(CAST(:devs AS uuid[]))
    """), {"devs": list(anc)})).mappings().all()
    sym = {"id": alarm["id"], "device_id": device_id, "alarm_type": alarm_type,
           "category": category, "metric_key": metric_key, "severity": alarm["severity"],
           "first_seen": alarm.get("first_seen") or datetime.now(UTC)}
    root = pick_root(graph, sym, [dict(r) for r in rows], await fire_rooms(session),
                     await representatives(session))
    if root is None:
        return None
    await mark(session, alarm_id=alarm["id"], root=root)
    return {**root, "layer": "cooling"}


async def sweep(session: AsyncSession) -> dict[str, int]:
    """Late roots and the end of the hold, once a minute.

    Adoption at raise time misses a root that arrives after its symptom - a
    rack probe trips before the CRAH's own alarm has finished its dwell - so
    every open, visible, eligible alarm is offered a root again here. And a
    root that cleared more than the hold ago releases what it still holds.
    """
    graph = await load_graph(session)
    now = datetime.now(UTC)
    visible = [dict(r) for r in (await session.execute(text(_OPEN_VISIBLE))).mappings().all()]
    fires = await fire_rooms(session)
    reps = await representatives(session)
    tied = set(reps.values())
    recent = now.timestamp() - 2 * LAG_S
    adopted = 0
    # Oldest first, so the first of a kind is the one that stays visible.
    for a in sorted(visible, key=lambda r: (_ts(r["first_seen"]), r["id"])):
        if _ts(a["first_seen"]) < recent:
            continue
        node = graph.nodes.get(a["device_id"])
        if node is None or not symptom_eligible(a["alarm_type"], a.get("category"), node.tier,
                                                a.get("metric_key")):
            continue
        root = pick_root(graph, a, visible, fires, reps)
        if root is None:
            continue
        if root["representative"]:
            reps.setdefault((root["id"], a["alarm_type"]), a["id"])
            if a["id"] in tied:
                continue                       # already tied, and still the visible one
        await mark(session, alarm_id=a["id"], root=root, now=now)
        tied.add(a["id"])
        adopted += 1

    released = (await session.execute(text("""
        UPDATE alarm s
           SET is_symptom = CASE WHEN (s.attributes->'l5'->>'suppressed')::boolean
                                 THEN false ELSE s.is_symptom END,
               root_cause_alarm_id = CASE WHEN (s.attributes->'l5'->>'suppressed')::boolean
                                          THEN NULL ELSE s.root_cause_alarm_id END,
               attributes = s.attributes - 'l5'
          FROM alarm r
         WHERE s.attributes ? 'l5'
           AND r.id = CAST(s.attributes->'l5'->>'root' AS uuid)
           AND (s.state = 'CLEARED' OR (r.state = 'CLEARED'
                AND r.cleared_at < now() - make_interval(secs => :hold)))
        RETURNING s.id::text AS id, s.device_id::text AS device_id,
                  s.severity::text AS severity, s.state::text AS state, r.id::text AS root
    """), {"hold": RESTORE_HOLD_S})).mappings().all()
    if adopted or released:
        log.info("cooling correlation sweep", adopted=adopted,
                 released=sum(1 for r in released if r["state"] != "CLEARED"))
    return {"adopted": adopted, "released": len(released),
            "released_open": sum(1 for r in released if r["state"] != "CLEARED")}
