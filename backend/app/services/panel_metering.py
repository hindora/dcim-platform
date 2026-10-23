"""A passive panel, read through the meter clamped on it.

The panel's own state stays what it is - nothing polls a cabinet of breakers -
and the figures here are the meter's, named as the meter's. The topology
service already borrows the same reading for the one-line diagram
(`_derive_power`, kind "metered"); this is the full view of it for the
device's own pages.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import panel_metering as repo
from app.repositories import telemetry as tele_repo
from app.schemas import DeviceMetering, MeteredCircuit, PanelMeter

# The freshness window the rest of the platform reads last-known state under
# (meter_channels.for_branches uses the same 15 minutes).
FRESH = timedelta(minutes=15)
_CIRCUIT = re.compile(r"^Ckt\d+$")
_PHASES = ("A", "B", "C")


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


def panel_meter(link: dict[str, Any], values: list[dict[str, Any]],
                schedule: dict[str, dict[str, Any]], now: datetime) -> PanelMeter:
    """One meter's readings, split into the panel's supply and its circuits.

    The EV2 reports the supply on the empty instance (power, energy, THD),
    per phase on A/B/C (current, voltage) and its power factor on PANEL; each
    CT channel is CktNN. A circuit is listed when the commissioning schedule
    names what it feeds or it is carrying load; the rest are spare breakers,
    counted rather than listed.
    """
    supply: dict[str, float] = {}
    phases: dict[str, dict[str, float]] = {"current": {}, "voltage_ln": {}}
    circuits: dict[str, dict[str, float]] = {}
    newest: datetime | None = None
    for v in values:
        val = v.get("value")
        # Alarm flags and words ride along in the same list; only numbers here.
        if val is None or isinstance(val, (bool, str)):
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        key, inst = v["metric"], v.get("instance") or ""
        ts = v.get("ts")
        if ts is not None and (newest is None or ts > newest):
            newest = ts
        if _CIRCUIT.match(inst):
            circuits.setdefault(inst, {})[key] = num
        elif inst in _PHASES and key in phases:
            phases[key][inst] = num
        elif inst in ("", "PANEL"):
            supply[key] = num

    rows: list[MeteredCircuit] = []
    spare = 0
    def order(s: str) -> int:
        return int(s[3:]) if s[3:].isdigit() else 0

    for inst in sorted(set(circuits) | set(schedule), key=order):
        c = circuits.get(inst, {})
        s = schedule.get(inst, {})
        named = bool(s.get("branch_id") or s.get("label"))
        if not named and not c.get("power_draw"):
            spare += 1
            continue
        rows.append(MeteredCircuit(
            instance=inst, power_w=c.get("power_draw"), current_a=c.get("current"),
            power_factor=c.get("power_factor"), energy_kwh=c.get("energy_consumed"),
            branch_id=s.get("branch_id"), branch_name=s.get("branch_name"), label=s.get("label"),
        ))

    status = link.get("status") or "UNKNOWN"
    stale = status != "ONLINE" or newest is None or now - newest > FRESH
    return PanelMeter(
        meter_id=link["meter_id"], meter_name=link["meter_name"], status=status,
        last_seen=link.get("last_seen"), as_of=newest, stale=stale,
        power_w=supply.get("power_draw", _f(link.get("power_w"))),
        energy_kwh=supply.get("energy_consumed"), power_factor=supply.get("power_factor"),
        frequency_hz=supply.get("line_frequency"),
        voltage_thd_pct=supply.get("voltage_thd_pct"),
        current_thd_pct=supply.get("current_thd_pct"),
        current_a=phases["current"], voltage_ln=phases["voltage_ln"],
        circuits=rows, spare_circuits=spare,
    )


async def device_metering(session: AsyncSession, device_id: str,
                          now: datetime | None = None) -> DeviceMetering:
    now = now or datetime.now(UTC)
    links = (await repo.meters_fed_from(session, [device_id])).get(device_id, [])
    meters = []
    for link in links:
        values = await tele_repo.latest_values(session, link["meter_id"])
        schedule = await repo.circuit_schedule(session, link["meter_id"])
        meters.append(panel_meter(link, values, schedule, now))
    return DeviceMetering(device_id=device_id, meters=meters)
