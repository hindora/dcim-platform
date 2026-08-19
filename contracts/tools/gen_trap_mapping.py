#!/usr/bin/env python3
"""Generate contracts/mappings/snmp/traps.yaml from a simulator checkout.

Run once at integration time, then COMMIT the output. This is not part of the
build: the DCIM must not depend on the simulator's source to compile.

    python contracts/tools/gen_trap_mapping.py --simulator ../DCIM/Datacenter_Network_Simulator

Why generate rather than hand-write
-----------------------------------
The device plane defines ~100 trap types on a placeholder enterprise tree and
rewrites each to the SENDING VENDOR's real MIB OID at transmit time. An
over-current leaves an APC rPDU as rPDUOverload (318.0.276) and a Raritan PX as
overCurrentProtectorSensorStateChange (13742.6.0.65). A receiver keyed on the
placeholder tree drops most traps, and several hundred vendor leaves cannot be
transcribed by hand and stay correct.

The output is keyed by WIRE OID, because that is all the receiver sees.

Severity and clears are DECLARED, not guessed
---------------------------------------------
Severity comes from TrapDefinition.severity rather than from the trap's name.
An earlier version of this script inferred both from substrings and decided
`linkUp` was a minor alarm rather than the clear of `linkDown` - which would
have meant link alarms never cleared and the alarm list only ever grew. Clear
pairing is now an explicit table below: it is longer, but it is reviewable and
cannot silently mis-read a name.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "contracts" / "mappings" / "snmp" / "traps.yaml"

# The simulator's severity vocabulary -> this platform's.
SEVERITY_MAP = {
    "informational": "INFO",
    "minor": "MINOR",
    "major": "MAJOR",
    "critical": "CRITICAL",
}

# clear trap name -> the raise trap name(s) it resolves.
#
# Explicit because a wrong entry here means an alarm that never clears, and a
# missing entry means a "clear" that raises a brand new alarm instead. One
# clear can resolve several raises: a PDU dropping back under threshold clears
# both the high and the critical load alarm.
CLEAR_PAIRS: dict[str, tuple[str, ...]] = {
    "linkUp": ("linkDown",),
    "bgpEstablished": ("bgpSessionDown",),
    "cpuNormal": ("cpuHighUsage", "cpuSustained"),
    "memoryNormal": ("memoryHighUsage",),
    "temperatureNormal": ("temperatureAlert", "cpuTempCritical"),
    "batteryNormal": ("upsLowBattery",),
    "batteryHealthRestored": ("batteryLowHealth", "batteryFailure"),
    "utilityPowerRestored": ("upsOnBattery",),
    "outputNormal": ("outputOverload",),
    "bypassCleared": ("bypassActive",),
    "inputVoltageNormal": ("inputVoltageHigh", "voltageHigh"),
    "inputVoltageLowCleared": ("inputVoltageLow", "voltageLow"),
    "pduLoadNormal": ("loadHigh", "loadCritical"),
    "pduTempNormal": ("pduTempHigh",),
    "pduHumidityNormal": ("pduHumidityHigh",),
    "pduFrequencyNormal": ("pduFrequencyFault", "frequencyOutOfRange"),
    "sensorAmbientTempNormal": ("sensorAmbientTempHigh", "sensorAmbientTempCritical"),
    "sensorMidTempNormal": ("sensorMidTempHigh",),
    "sensorOutletTempNormal": ("sensorOutletTempHigh",),
    "sensorHumidityNormal": ("sensorHighHumidity", "sensorCriticalHumidity",
                             "sensorLowHumidity", "humidityAlert"),
    "sensorAirflowNormal": ("sensorHighAirflow", "sensorLowAirflow", "airflowAlert"),
    "sensorDewPointNormal": ("dewPointAlert",),
    "generatorFuelNormal": ("generatorLowFuel",),
    "generatorCoolantNormal": ("generatorLowCoolant",),
    "generatorTempNormal": ("generatorTempHigh",),
    "generatorBatteryNormal": ("generatorBatteryFailure",),
    "generatorTransferCleared": ("generatorTransferSwitch",),
    "atsTransferNormal": ("atsTransferEmergency", "atsSourceLost"),
    "atsTransferFaultCleared": ("atsFailToTransfer",),
    "switchgearBusNormal": ("switchgearBusFault",),
}

# warmStart and coldStart are NOT clears: a device restarting is its own event,
# and treating it as a clear would silently resolve unrelated alarms.
RESTART_TRAPS = {"coldStart", "warmStart"}

EVENT_TYPE_OVERRIDES = {
    "coldStart": "device_restarted",
    "warmStart": "device_restarted",
    "authenticationFailure": "auth_failure",
    "loadHigh": "pdu_load_high",
    "loadCritical": "pdu_load_critical",
    "outputOverload": "ups_output_overload",
    "upsOnBattery": "ups_on_battery",
    "upsLowBattery": "ups_battery_low",
}

SEVERITY_RANK = {"CLEAR": 0, "INFO": 1, "WARNING": 2, "MINOR": 3,
                 "MAJOR": 4, "CRITICAL": 5}


def event_type_for(name: str) -> str:
    if name in EVENT_TYPE_OVERRIDES:
        return EVENT_TYPE_OVERRIDES[name]
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--simulator", required=True,
                    help="path to the Datacenter_Network_Simulator checkout")
    args = ap.parse_args()

    sim = Path(args.simulator).resolve()
    if not (sim / "core" / "vendor_oids.py").exists():
        sys.exit(f"no core/vendor_oids.py under {sim}")
    sys.path.insert(0, str(sim))

    from core.trap_definitions import TRAP_DEFINITIONS  # noqa: E402
    from core.vendor_oids import VENDOR_TRAPS  # noqa: E402

    known = {t.value for t in TRAP_DEFINITIONS}

    # A pair naming a trap this plane does not define is a typo that would
    # silently do nothing. Fail rather than emit it.
    for clear, raises in CLEAR_PAIRS.items():
        if clear not in known:
            sys.exit(f"CLEAR_PAIRS names unknown clear trap {clear!r}")
        for r in raises:
            if r not in known:
                sys.exit(f"CLEAR_PAIRS[{clear!r}] names unknown raise trap {r!r}")

    def entry_for(name: str, declared: str) -> dict:
        if name in CLEAR_PAIRS:
            targets = [event_type_for(r) for r in CLEAR_PAIRS[name]]
            return {"event_type": targets[0], "severity": "CLEAR",
                    "is_clear": True, "clears": targets, "name": name}
        return {"event_type": event_type_for(name),
                "severity": SEVERITY_MAP.get(declared, "MINOR"),
                "is_clear": False, "clears": [], "name": name}

    by_oid: dict[str, list[dict]] = defaultdict(list)

    for trap, defn in TRAP_DEFINITIONS.items():
        if defn.oid:
            e = entry_for(trap.value, defn.severity)
            e["vendor"] = "synthetic"
            by_oid[defn.oid].append(e)

    for vendor, table in VENDOR_TRAPS.items():
        for trap, oid in table.items():
            defn = TRAP_DEFINITIONS.get(trap)
            if defn is None:
                continue
            e = entry_for(trap.value, defn.severity)
            e["vendor"] = vendor
            by_oid[oid].append(e)

    lines = [
        "# SNMP trap mappings: WIRE OID -> canonical event.",
        "#",
        "# GENERATED by contracts/tools/gen_trap_mapping.py - do not hand-edit;",
        "# regenerate against the device plane and commit the result.",
        f"# Generated {datetime.now(UTC).date().isoformat()}.",
        "#",
        "# Keyed by the OID that arrives on the wire, because that is all the",
        "# receiver sees. Real gear keys notifications off the VENDOR, not off the",
        "# alarm's meaning: an over-current is rPDUOverload (318.0.276) on an APC",
        "# rPDU and overCurrentProtectorSensorStateChange (13742.6.0.65) on a",
        "# Raritan PX. A receiver that knows only one enterprise tree drops most",
        "# traps.",
        "#",
        "# `is_clear: true` entries carry `clears:` - the event types they",
        "# resolve. That is what lets a clear reach the same alarm key as its",
        "# raise without the backend ever parsing an OID, and it lets one clear",
        "# resolve a family (a PDU dropping below threshold clears both the high",
        "# and the critical load alarm).",
        "#",
        "# Severity is the vendor's DECLARED severity, never inferred from the",
        "# trap's name.",
        "",
        "version: 1",
        "",
        "traps:",
    ]

    ambiguous = clears = 0
    for oid in sorted(by_oid, key=lambda o: [int(p) for p in o.split(".") if p.isdigit()]):
        entries = by_oid[oid]
        uniq = {(e["event_type"], e["severity"], e["is_clear"]): e for e in entries}
        # Prefer a clear over a raise on a shared OID: failing to clear leaves a
        # stuck alarm, which is worse than an extra clear attempt that finds
        # nothing to resolve.
        chosen = max(uniq.values(),
                     key=lambda e: (e["is_clear"], SEVERITY_RANK.get(e["severity"], 0)))
        vendors = sorted({e["vendor"] for e in entries})

        lines.append(f"  - oid: {oid}")
        lines.append(f"    event_type: {chosen['event_type']}")
        lines.append(f"    severity: {chosen['severity']}")
        if chosen["is_clear"]:
            clears += 1
            lines.append("    is_clear: true")
            joined = ", ".join(chosen["clears"])
            lines.append(f"    clears: [{joined}]")
        lines.append(f"    # {chosen['name']} [{', '.join(vendors)}]")
        if len(uniq) > 1:
            ambiguous += 1
            others = sorted(f"{e['name']}->{e['event_type']}({e['severity']})"
                            for e in uniq.values() if e is not chosen)
            lines.append("    # AMBIGUOUS - this OID also carries: " + "; ".join(others))
        lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(by_oid)} wire OIDs, {clears} clears, "
          f"{ambiguous} ambiguous, from {len(TRAP_DEFINITIONS)} trap definitions "
          f"and {len(VENDOR_TRAPS)} vendor tables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
