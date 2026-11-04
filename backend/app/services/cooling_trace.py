"""How a device is cooled, and the state of each machine on the way.

The power page already answers "what feeds this server". This answers the
same question for heat: where it goes, through what, and whether each hop is
doing its job right now.

HOW THE COOLING METHOD IS KNOWN
- `device.attributes.cooling_method`, when the asset record says so: 'air',
  'liquid' (direct-to-chip cold plates), 'immersion' or 'rdhx' (rear-door).
  The model decides it on a real site, and the inventory is the authority.
- Otherwise the drawing: a cooling-layer connection to a CDU means direct
  liquid. No connection means air.

A direct-to-chip server is still partly AIR-cooled - cold plates take about
70 % of its heat (CPU and GPU); its memory, drives, regulators and supplies
are cooled by room air - so its trace has both halves: the CDU loop for the
chips and the room's air units for the rest. A CRAH outage still heats its
inlet like any other server's; a CDU outage heats its CPU within a minute.

The air side is tied by room: every air unit in the hall is taken to serve
every rack in it until rack-to-unit influence is measured (docs/33 Phase 4),
and the response says `basis: "room"` so the page can say so too.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.alarms import cooling_correlation as cc

METHODS = ("air", "liquid", "immersion", "rdhx")

#: Share of a direct-to-chip server's heat the cold plates capture
#: (the simulator's core.rack_capacity.DTC_AIR_FRACTION is the remainder).
DTC_LIQUID_SHARE = 0.70

_PLANT_TYPES = ("chiller", "pump", "cooling_tower")


async def liquid_cooled(session: AsyncSession, device_ids: list[str]) -> set[str]:
    """Which of these devices sit on a CDU cold-plate loop (drawn), or are
    declared liquid in their asset record."""
    if not device_ids:
        return set()
    rows = (await session.execute(text("""
        SELECT d.id::text
          FROM device d
         WHERE d.id = ANY(CAST(:ids AS uuid[]))
           AND (coalesce(d.attributes->>'cooling_method', '') IN ('liquid', 'immersion')
                OR (coalesce(d.attributes->>'cooling_method', '') = ''
                    AND EXISTS (
                        SELECT 1 FROM connection c
                          JOIN device u ON u.id = CASE WHEN c.a_device_id = d.id
                                                       THEN c.b_device_id ELSE c.a_device_id END
                         WHERE c.layer = 'cooling' AND u.device_type = 'cdu'
                           AND (c.a_device_id = d.id OR c.b_device_id = d.id))))
    """), {"ids": device_ids})).scalars().all()
    return set(rows)


def _unit(u: dict[str, Any]) -> dict[str, Any]:
    keep = ("device_id", "name", "state", "reason", "supply_c", "return_c", "setpoint_c",
            "delta_t_k", "running", "flow_l_s", "pump_pct", "fan_pct", "heat_kw",
            "rated_kw", "duty_pct", "alarms_open")
    return {k: u.get(k) for k in keep if k in u}


async def device_cooling(session: AsyncSession, device_id: str) -> dict[str, Any] | None:
    dev = (await session.execute(text("""
        SELECT d.id::text AS id, d.name, d.device_type::text AS device_type,
               d.room_id::text AS room_id, r.name AS room_name,
               r.datacenter_id::text AS datacenter_id, d.attributes->>'cooling_method' AS declared
          FROM device d LEFT JOIN room r ON r.id = d.room_id
         WHERE d.id = CAST(:id AS uuid)
    """), {"id": device_id})).mappings().first()
    if dev is None:
        return None
    graph = await cc.load_graph(session)
    cdus = sorted(m for m in graph.cooling_nbrs.get(device_id, ())
                  if graph.nodes.get(m) and graph.nodes[m].device_type == "cdu")
    declared = (dev["declared"] or "").strip().lower()
    if declared in METHODS:
        method, basis = declared, "declared"
    else:
        method, basis = ("liquid" if cdus else "air"), ("drawn" if cdus else "default")

    from app.services import plant as plant_service
    from app.services import thermal as thermal_service

    room = (await thermal_service.room_view(session, dev["room_id"])
            if dev["room_id"] else {})
    cdu_rows = {u["device_id"]: u for u in room.get("cdu_units", [])}
    liquid = None
    if method == "liquid":
        loops = []
        for cid in cdus:
            row = cdu_rows.get(cid)
            loops.append(_unit(row) if row else
                         {"device_id": cid, "name": graph.nodes[cid].name, "state": None})
        liquid = {"share": DTC_LIQUID_SHARE, "cdus": loops}

    air_units = [_unit(u) for u in room.get("crah_units", [])]
    air = {
        "basis": "room",
        "room_name": dev["room_name"],
        "units": air_units,
        "running": sum(1 for u in air_units if u.get("running")),
        "supply_c": room.get("room_supply_c"),
        # Of the server's heat, what the room's air has to carry.
        "share": (1.0 - DTC_LIQUID_SHARE) if method == "liquid" else 1.0,
    }

    plant = {"machines": [], "chw_supply_c": None}
    if dev["datacenter_id"]:
        eq = (await plant_service.plant(session, view="facility")).get("equipment", [])
        here = [m for m in eq if m.get("site_id") == dev["datacenter_id"]
                and m.get("device_type") in _PLANT_TYPES]
        plant["machines"] = [{
            "device_id": m["device_id"], "name": m["name"], "device_type": m["device_type"],
            "running": m.get("running"), "verdict": m.get("verdict"),
            "supply_c": m.get("supply_c"), "alarms_open": m.get("alarms_open", 0),
        } for m in sorted(here, key=lambda m: (_PLANT_TYPES.index(m["device_type"]), m["name"]))]
        sup = [m["supply_c"] for m in here
               if m["device_type"] == "chiller" and m.get("running")
               and m.get("supply_c") is not None]
        plant["chw_supply_c"] = round(min(sup), 1) if sup else None

    return {"device_id": device_id, "name": dev["name"], "device_type": dev["device_type"],
            "method": method, "method_basis": basis,
            "liquid": liquid, "air": air, "plant": plant}
