"""Device actions: switch a PDU outlet, restart a server through its BMC.

A DCIM that can only watch is half a tool, and one that can write carelessly
is a hazard, so this is built around what real sites do:

- The write is made by the COLLECTOR that owns the device's endpoint, over the
  device's own management protocol (SNMP SET on the PDU's control OID, a
  Redfish ComputerSystem.Reset on the server's BMC). The platform holds no
  usable credentials and is not on the management network; the collector is.
  The command rides the queue upgrades use.
- Authority follows what a wrong click costs. Restoring power is an operator's;
  taking something down needs a typed confirmation of the device's name, and
  the ones that cut power outright (outlet off, cycle, force off) an admin.
- Before an outlet is opened the operator is shown what it takes down: a load
  with no other live feed goes dark, a dual-corded one only loses redundancy.
- One action at a time per device, a short timeout, and an audit row naming who,
  what and why.
- Plant setpoints (CRAH, CDU, chiller) are deliberately NOT here: they belong to
  the BMS, which holds the interlocks and the change log (see docs).

Where a write would be refused by the hardware the platform says so up front
(a metered strip has no relay) instead of offering a button that fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.security import ROLES
from app.repositories import actions as repo
from app.services.collector import ownership

_RANK = {r: i for i, r in enumerate(ROLES)}


@dataclass(frozen=True)
class Action:
    id: str
    label: str
    kind: str                    # "outlet" | "power"
    role: str                    # lowest role that may run it
    confirm: bool                # the device's name must be typed
    command: str | None = None   # outlet: on | off | cycle
    reset: str | None = None     # power: the Redfish ResetType
    effect: str = ""


ACTIONS: dict[str, Action] = {a.id: a for a in (
    Action("outlet_on", "Switch on", "outlet", "operator", False, command="on",
           effect="Restores power to whatever is corded to this outlet."),
    Action("outlet_off", "Switch off", "outlet", "admin", True, command="off",
           effect="Cuts power to this outlet at the relay."),
    Action("outlet_cycle", "Power cycle", "outlet", "admin", True, command="cycle",
           effect="Opens the outlet, holds it a few seconds, closes it again."),
    Action("power_on", "Power on", "power", "operator", False, reset="On",
           effect="Powers the server on through its BMC."),
    Action("graceful_shutdown", "Shut down", "power", "operator", True,
           reset="GracefulShutdown",
           effect="Asks the operating system to shut down cleanly, then powers off."),
    Action("graceful_restart", "Restart", "power", "operator", True,
           reset="GracefulRestart",
           effect="Asks the operating system to restart cleanly."),
    Action("force_restart", "Force restart", "power", "admin", True, reset="ForceRestart",
           effect="Resets the server without warning the operating system."),
    Action("power_cycle", "Power cycle", "power", "admin", True, reset="PowerCycle",
           effect="Removes and restores the server's power without warning."),
    Action("force_off", "Force off", "power", "admin", True, reset="ForceOff",
           effect="Cuts the server's power without warning the operating system."),
)}

#: Rack PDU SKUs by whether their outlets have a relay. Mirrors what the
#: hardware does: a metered strip has no control column at all.
_SWITCHING = {"ap8941": True, "ap8959": True, "px2-5170cr": True, "px3-5878": True,
              "px3-5190r": True, "px3-5161r": True,
              "ap8886": False, "ap8865": False, "ap8681": False}

#: (OID prefix, {command: integer}) per vendor family, from the vendors' MIBs.
#:   APC    rPDU2OutletSwitchedControlCommand  1 immediateOn, 2 immediateOff, 3 immediateReboot
#:   Raritan PX2 outletSwitchingOperation      0 off, 1 on, 2 cycle
_OUTLET_MIBS = {
    "apc": ("1.3.6.1.4.1.318.1.1.26.9.2.4.1.5", {"on": 1, "off": 2, "cycle": 3}),
    "raritan": ("1.3.6.1.4.1.13742.6.4.1.2.1.2.1", {"on": 1, "off": 0, "cycle": 2}),
}


class ActionError(Exception):
    def __init__(self, message: str, code: int = 422) -> None:
        super().__init__(message)
        self.code = code


def switching(model: str | None) -> bool | None:
    """True if the SKU's outlets have relays, False if metered, None unknown."""
    m = (model or "").lower()
    for key, v in _SWITCHING.items():
        if key in m:
            return v
    return None


def vendor_family(vendor: str | None) -> str | None:
    v = (vendor or "").lower()
    if "raritan" in v:
        return "raritan"
    if "apc" in v or "schneider" in v:
        return "apc"
    return None


def outlet_oid(vendor: str | None, outlet: int, command: str) -> tuple[str, int]:
    fam = vendor_family(vendor)
    if fam is None:
        raise ActionError(
            f"there is no known outlet-control MIB for {vendor or 'an unknown make'}: "
            f"this platform speaks APC and Raritan")
    prefix, values = _OUTLET_MIBS[fam]
    return f"{prefix}.{outlet}", values[command]


def device_class(device_type: str) -> str | None:
    if device_type == "pdu":
        return "pdu"
    if device_type in ("server", "storage"):
        return "server"
    return None


def allowed(role: str, action: Action) -> bool:
    return _RANK.get(role, 0) >= _RANK[action.role]


def _survives(row: dict[str, Any], pdu_id: str) -> str:
    """'dark' | 'degraded' for a load on an outlet that is about to open.

    It keeps running only if some OTHER cord is fed by a different, live device:
    a second cord into the same strip is the same failure, and a feed that is
    already offline is no feed at all."""
    others = [c for c in row["cords"]
              if c["feed_id"] != pdu_id and c["feed_status"] not in ("OFFLINE",)]
    return "degraded" if others else "dark"


async def capabilities(session, device_id: str, role: str) -> dict[str, Any] | None:
    d = await repo.device_basics(session, device_id)
    if d is None:
        return None
    cls = device_class(d["device_type"])
    out: dict[str, Any] = {
        "device": d, "class": cls, "available": False, "reason": None,
        "endpoint": None, "actions": [], "outlets": [],
    }
    if cls is None:
        out["reason"] = "this kind of device has no remote actions"
        return out
    if d["lifecycle"] in ("decommissioned", "retired"):
        out["reason"] = f"the device is {d['lifecycle']}"
        return out

    # What the hardware can do comes before what the platform can reach: a
    # metered strip answers "no relay", whatever its endpoint looks like.
    sw = None
    if cls == "pdu":
        sw = switching(d["model"])
        if sw is False:
            out["reason"] = (f"{d['model']} is a metered strip: it measures, but its "
                             f"outlets have no relay to open")
            return out
        if vendor_family(d["vendor"]) is None:
            out["reason"] = (f"no known outlet-control MIB for {d['vendor'] or 'this make'} "
                             f"(APC and Raritan are supported)")
            return out

    wanted = ("snmp", "native_card") if cls == "pdu" else ("redfish", "bmc")
    eps = [e for e in await repo.endpoints_with_owner(session, device_id)
           if e["enabled"] and e["admin_state"] == "enabled"
           and (e["protocol"], e["role"]) == wanted]
    if not eps:
        out["reason"] = ("no enabled SNMP management card to write to" if cls == "pdu"
                         else "no enabled Redfish BMC endpoint to write to")
        return out
    ep = eps[0]
    # The collector that actually owns it now: the pin, then the assigner's
    # record, then the live plan - the one answer every reader of ownership
    # shares. The recorded owner alone is empty on a fleet that has never
    # failed over, and would have said nobody owns a device that is polled
    # every minute.
    owner = (await ownership(session)).get(ep["id"]) or ep["owner"]
    out["endpoint"] = {"id": ep["id"], "protocol": ep["protocol"], "role": ep["role"],
                       "owner": owner}
    if owner is None:
        out["reason"] = "no collector owns this device's endpoint yet"
        return out

    if cls == "pdu":
        loads = await repo.outlet_loads(session, device_id)
        out["outlets"] = [{
            "outlet": r["outlet"], "connector": r["connector"], "branch": r["branch"],
            "load": ({"id": r["load_id"], "name": r["load_name"], "type": r["load_type"],
                      "psu": r["psu"], "impact": _survives(r, device_id)}
                     if r["load_id"] else None),
        } for r in loads]
        out["switching_known"] = sw is True
    out["available"] = True
    out["actions"] = [{
        "id": a.id, "label": a.label, "kind": a.kind, "role": a.role,
        "confirm": a.confirm, "effect": a.effect, "allowed": allowed(role, a),
    } for a in ACTIONS.values() if a.kind == ("outlet" if cls == "pdu" else "power")]
    return out


async def preview(session, device_id: str, action_id: str, outlet: int | None,
                  role: str) -> dict[str, Any]:
    """What this action would do, said before anyone does it."""
    cap = await capabilities(session, device_id, role)
    if cap is None:
        raise ActionError("device not found", 404)
    act = ACTIONS.get(action_id)
    if act is None:
        raise ActionError(f"unknown action {action_id!r}")
    if not cap["available"]:
        raise ActionError(cap["reason"] or "no actions are available", 409)
    name = cap["device"]["name"]
    out: dict[str, Any] = {"action": act.id, "label": act.label, "effect": act.effect,
                           "confirm": act.confirm, "confirm_name": name,
                           "allowed": allowed(role, act), "needs_role": act.role,
                           "dark": [], "degraded": []}
    if act.kind == "power":
        out["dark"] = [{"id": cap["device"]["id"], "name": name, "why": "the server itself"}]
        return out
    row = next((o for o in cap["outlets"] if o["outlet"] == outlet), None)
    if row is None:
        raise ActionError(f"{name} has no outlet {outlet}")
    out["outlet"] = outlet
    load = row["load"]
    if load is None:
        out["note"] = "Nothing is recorded as corded to this outlet."
    elif act.command == "on":
        out["note"] = f"{load['name']} regains this cord."
    elif load["impact"] == "dark":
        out["dark"] = [{"id": load["id"], "name": load["name"],
                        "why": "this is its only live cord"}]
    else:
        out["degraded"] = [{"id": load["id"], "name": load["name"],
                            "why": "it keeps running on its other feed, without redundancy"}]
    return out


def build_payload(cap: dict[str, Any], act: Action, outlet: int | None,
                  actor: str, reason: str | None) -> dict[str, Any]:
    d, ep = cap["device"], cap["endpoint"]
    base = {"device_id": d["id"], "device_name": d["name"], "endpoint_id": ep["id"],
            "action": act.id, "requested_by": actor, "reason": reason}
    if act.kind == "outlet":
        oid, value = outlet_oid(d["vendor"], outlet or 0, act.command or "")
        return base | {"op": "snmp_set", "oid": oid, "type": "integer", "value": value,
                       "outlet": outlet, "label": f"outlet {outlet} {act.command}"}
    return base | {"op": "redfish_action", "action_path": "Actions/ComputerSystem.Reset",
                   "body": {"ResetType": act.reset}, "label": act.label.lower()}


async def submit(session, *, device_id: str, action_id: str, outlet: int | None,
                 confirm: str | None, reason: str | None, actor: str,
                 role: str) -> dict[str, Any]:
    cap = await capabilities(session, device_id, role)
    if cap is None:
        raise ActionError("device not found", 404)
    act = ACTIONS.get(action_id)
    if act is None:
        raise ActionError(f"unknown action {action_id!r}")
    if not allowed(role, act):
        raise ActionError(f"{act.label.lower()} needs {act.role} access", 403)
    if not cap["available"]:
        raise ActionError(cap["reason"] or "no actions are available", 409)
    if act.kind == "outlet" and (
            outlet is None or outlet not in {o["outlet"] for o in cap["outlets"]}):
        raise ActionError(f"{cap['device']['name']} has no outlet {outlet}")
    name = cap["device"]["name"]
    if act.confirm and (confirm or "").strip().lower() != name.lower():
        raise ActionError(f"type the device's name, {name}, to confirm")
    if await repo.open_action(session, device_id):
        raise ActionError("an action on this device is still in progress", 409)
    payload = build_payload(cap, act, outlet, actor, (reason or "").strip()[:300] or None)
    command_id = await repo.create_action_command(
        session, collector_id=cap["endpoint"]["owner"], payload=payload, actor=actor)
    return {"command_id": command_id, "collector": cap["endpoint"]["owner"],
            "payload": payload}
