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
import re
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
    # A relay closing again, and a chassis coming back up. Both were missing,
    # and both did exactly what the comment above this table predicts: the
    # recovery raised a brand new alarm instead of resolving anything. A PDU
    # left the console reading "Outlet Off MAJOR" and "Outlet On INFO" side by
    # side - the fault that had ended, and beside it the news that it had
    # ended, filed as a second thing to look at.
    "outletOn": ("outletOff",),
    "serverPowerOn": ("serverPowerOff",),
    # "Main breaker reclosed - bus re-energized" against "Main breaker
    # tripped - bus de-energized downstream". The third one found by the same
    # audit, and the most expensive to leave: a tripped main is a critical
    # alarm, and the reclose that ends it was arriving as informational news
    # beside it.
    "switchgearBreakerClosed": ("switchgearBreakerTrip",),
}

# warmStart and coldStart are NOT clears: a device restarting is its own event,
# and treating it as a clear would silently resolve unrelated alarms.
RESTART_TRAPS = {"coldStart", "warmStart"}

#: Recovery-SHAPED names that really are their own event rather than a clear.
#:
#: The generator refuses to emit a trap whose name reads like a recovery and
#: which resolves nothing, because that combination has now produced the same
#: bug twice: the "clear" arrives, matches no open alarm, and is filed as a new
#: one. Anything genuinely in that shape goes here, deliberately and by name,
#: so the decision is visible rather than implied by an absence.
NOT_A_CLEAR = RESTART_TRAPS | {
    # A link coming up on a device that was never reported down is news in its
    # own right on this plane - the OOB switch rules raise it separately.
    "oobSwitchLinkUp",
}

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


def _lower_first(name: str) -> str:
    """`FanUnderSpeed` -> `fanUnderSpeed`.

    The rules spell their names CamelCase and the trap catalogue spells the
    same conditions camelCase. Without this the two vocabularies produce
    `_fan_under_speed` and `fan_under_speed` for one condition.
    """
    return name[:1].lower() + name[1:] if name else name


def snake(name: str) -> str:
    """`HighCPUSustained` -> `high_cpu_sustained`, acronyms intact.

    `event_type_for` splits before every capital, which is right for camelCase
    and wrong for the rules: they carry CPU, UPS, PDU, MPP, MCC, CHW, CRAH.
    Splitting per letter produced `high_c_p_u_sustained`, which is not a name
    anybody would search for or recognise in an alarm list.
    """
    out = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    out = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", out)
    return out.lower()


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

    from core.trap_definitions import TRAP_DEFINITIONS, TrapType  # noqa: E402
    from core.trap_rules import DEFAULT_RULES  # noqa: E402
    from core.vendor_oids import (  # noqa: E402
        CISCO,
        CISCO_ENV_STATE,
        LIEBERT,
        RARITAN,
        RARITAN_SENSOR_STATE,
        RARITAN_SENSOR_TYPE,
        VENDOR_TRAPS,
    )

    known = {t.value for t in TRAP_DEFINITIONS}

    # A pair naming a trap this plane does not define is a typo that would
    # silently do nothing. Fail rather than emit it.
    for clear, raises in CLEAR_PAIRS.items():
        if clear not in known:
            sys.exit(f"CLEAR_PAIRS names unknown clear trap {clear!r}")
        for r in raises:
            if r not in known:
                sys.exit(f"CLEAR_PAIRS[{clear!r}] names unknown raise trap {r!r}")


    # Which kinds of device can send each condition, from the rules that
    # dispatch it. A rule with no device_types fires on anything, and that
    # widest answer wins for the condition as a whole - narrowing it would drop
    # traps from equipment the rule does accept.
    #
    # This is what tells two meanings of one OID apart. A Liebert card sends
    # 476.1.42.3.3.0.5 for a charger failure and for a fan failure; the OID
    # cannot say which, and the sending device can.
    device_types_for: dict[str, set[str]] = {}
    for rule in DEFAULT_RULES:
        name = _lower_first(rule.rule_name)
        types = set(rule.device_types or ())
        if name in device_types_for:
            if not types or not device_types_for[name]:
                device_types_for[name] = set()
            else:
                device_types_for[name] |= types
        else:
            device_types_for[name] = types

    display_names = {t.value: (d.display_name or "")
                     for t, d in TRAP_DEFINITIONS.items()}

    # What each notification MEASURES, and where it puts the numbers.
    #
    # Read off the plane's own varbind builder rather than guessed. A threshold
    # trap nearly always ships the reading and the line it crossed - this plane
    # uses adjacent enterprise varbinds, Cisco ships cpmCPUTotal5minRev beside
    # its rising threshold - and carrying them through is what lets an alarm
    # raised by a notification be verified later against polled telemetry.
    #
    # Without it a lost recovery trap can only be resolved by a timer, and the
    # alarm reaches an operator with no number on it at all.
    _V = "1.3.6.1.4.1.99999.2"
    MEASURES = {
        "cpuHighUsage":   ("cpu_utilization", f"{_V}.1", f"{_V}.5"),
        "cpuSustained":   ("cpu_utilization", f"{_V}.1", f"{_V}.5"),
        "cpuNormal":      ("cpu_utilization", f"{_V}.1", f"{_V}.5"),
        "memoryHighUsage": ("memory_utilization", f"{_V}.2", f"{_V}.6"),
        "memoryNormal":   ("memory_utilization", f"{_V}.2", f"{_V}.6"),
        "cpuTempCritical": ("cpu_temperature", f"{_V}.3", f"{_V}.7"),
        "temperatureAlert": ("cpu_temperature", f"{_V}.3", f"{_V}.7"),
        "temperatureNormal": ("cpu_temperature", f"{_V}.3", f"{_V}.7"),
        "sensorAmbientTempHigh": ("ambient_temperature", f"{_V}.3", f"{_V}.7"),
        "sensorAmbientTempCritical": ("ambient_temperature", f"{_V}.3", f"{_V}.7"),
        "sensorAmbientTempNormal": ("ambient_temperature", f"{_V}.3", f"{_V}.7"),
        # Cisco puts the same two numbers on its own objects.
        "_cisco_cpu": ("cpu_utilization", CISCO["cpu5min"], CISCO["cpuRisingThresh"]),
    }

    def measurement(name: str, vendor: str) -> tuple[str, str, str] | None:
        if vendor == "cisco" and name in ("cpuHighUsage", "cpuSustained",
                                          "cpuNormal"):
            return MEASURES["_cisco_cpu"]
        return MEASURES.get(name)

    def entry_for(name: str, declared: str, vendor: str = "synthetic") -> dict:
        if name in CLEAR_PAIRS:
            targets = [event_type_for(r) for r in CLEAR_PAIRS[name]]
            m = measurement(name, vendor)
            return {"event_type": targets[0], "severity": "CLEAR",
                    "is_clear": True, "clears": targets, "name": name,
                    "display_name": display_names.get(name, ""),
                    "measures": m,
                    "device_types": sorted(device_types_for.get(name, ()))}
        return {"event_type": event_type_for(name),
                "severity": SEVERITY_MAP.get(declared, "MINOR"),
                "is_clear": False, "clears": [], "name": name,
                "display_name": display_names.get(name, ""),
                "measures": measurement(name, vendor),
                "device_types": sorted(device_types_for.get(name, ()))}

    by_oid: dict[str, list[dict]] = defaultdict(list)

    # How each vendor says WHICH condition, when its OID does not.
    #
    # Read off the plane's own trap payloads rather than guessed: a Liebert
    # condition trap carries lgpConditionDescr - "<device>: <display name>" -
    # and a Cisco environmental notification carries the state enumeration.
    # Those are the varbinds the vendors intended as the discriminator, and
    # they are the only thing that separates several meanings of one OID sent
    # by one kind of device.
    _CISCO_AMBIENT = {TrapType.TEMPERATURE_ALERT, TrapType.CPU_TEMP_CRITICAL,
                      TrapType.TEMPERATURE_NORMAL}

    # Raritan says WHICH condition with two varbinds, never one: an inlet
    # sensor notification carries the sensor type and the state it moved to, so
    # "current, above upper warning" and "current, above upper critical" are a
    # load alarm and a critical load alarm on the same OID from the same PDU.
    _ST, _SS = RARITAN_SENSOR_TYPE, RARITAN_SENSOR_STATE
    _RARITAN_PAIRS = {
        TrapType.PDU_LOAD_HIGH: ("current", "aboveUpperWarning"),
        TrapType.PDU_LOAD_CRITICAL: ("current", "aboveUpperCritical"),
        TrapType.PDU_OUTLET_CURRENT_HIGH: ("current", "aboveUpperCritical"),
        TrapType.PDU_LOAD_NORMAL: ("current", "normal"),
        TrapType.PDU_BREAKER_TRIPPED: ("trip", "open"),
        TrapType.PDU_VOLTAGE_HIGH: ("voltage", "aboveUpperCritical"),
        TrapType.PDU_VOLTAGE_LOW: ("voltage", "belowLowerCritical"),
        TrapType.PDU_FREQUENCY_FAULT: ("frequency", "aboveUpperWarning"),
        TrapType.PDU_FREQUENCY_NORMAL: ("frequency", "normal"),
    }

    def varbind_match(vendor: str, trap, defn) -> list[dict] | None:
        if vendor == "liebert":
            # The engine writes display_name when there is one and the raw trap
            # name when there is not; matching has to follow the same rule or
            # the entries with no display name never match anything.
            phrase = defn.display_name or trap.value
            return [{"oid": LIEBERT["conditionDescr"], "contains": phrase}]
        if vendor == "cisco" and trap in _CISCO_AMBIENT:
            state = ("normal" if trap == TrapType.TEMPERATURE_NORMAL else
                     "critical" if trap == TrapType.CPU_TEMP_CRITICAL else
                     "warning")
            return [{"oid": CISCO["envTempState"],
                     "equals_int": CISCO_ENV_STATE[state]}]
        if vendor == "raritan" and trap in _RARITAN_PAIRS:
            sensor, state = _RARITAN_PAIRS[trap]
            # The state lives in a table-specific column - inlet, outlet or
            # unit - and the engine picks the table from the condition, so the
            # matcher has to name the same one.
            table = ("outlet" if trap == TrapType.PDU_OUTLET_CURRENT_HIGH else
                     "unit" if trap == TrapType.PDU_BREAKER_TRIPPED else "inlet")
            return [
                {"oid": RARITAN["typeOfSensor"], "equals_int": _ST[sensor]},
                {"oid": RARITAN[f"{table}State"], "equals_int": _SS[state]},
            ]
        return None

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
            e = entry_for(trap.value, defn.severity, vendor)
            e["vendor"] = vendor
            e["match_varbind"] = varbind_match(vendor, trap, defn)
            by_oid[oid].append(e)

    # ---- the transmit path -------------------------------------------------
    #
    # TRAP_DEFINITIONS is the plane's CATALOGUE; DEFAULT_RULES is what actually
    # dispatches. They disagree by 57 OIDs, and the disagreement is not random:
    # it is almost entirely RECOVERY traps - FanUnderSpeed, LinkFlapCleared,
    # UPSFanNormal, PDUBreakerReset, CPUSustainedNormal. Generating from the
    # catalogue alone meant this platform could hear every alarm and none of the
    # all-clears. Measured on the wire: a CPU recovery arrived as
    # 1.3.6.1.4.1.99999.1.37, matched nothing, and became an `unknown_trap`
    # alarm that nothing could ever resolve.
    #
    # The rules carry `recovery_of`, so their clear pairing is DECLARED rather
    # than paired by hand in CLEAR_PAIRS above - which is why these entries can
    # be generated at all. A rule naming a recovery target that does not exist
    # is a typo that would produce a clear resolving nothing, so it fails here.


    def event_type_of_rule(rule) -> str:
        """What this platform should call the condition this rule reports.

        The catalogue's vocabulary wins where the catalogue has the condition:
        `HighCPU` and `cpuHighUsage` are one fact, and giving them two event
        types would rebuild, one layer down, the duplicate-alarm problem this
        platform just finished removing.

        A rule whose OID the catalogue already carries is answered by the entry
        that OID produced, rather than by re-deriving a name from the rule -
        that is exact where a name match is a guess.
        """
        raised = [e for e in by_oid.get(rule.trap_oid, []) if not e["is_clear"]]
        if raised:
            return raised[0]["event_type"]
        if _lower_first(rule.rule_name) in known_names:
            return event_type_for(_lower_first(rule.rule_name))
        return snake(rule.rule_name)

    by_rule_name = {r.rule_name: r for r in DEFAULT_RULES}
    known_names = {t.value for t in TRAP_DEFINITIONS}

    for rule in DEFAULT_RULES:
        if not rule.trap_oid or rule.trap_oid in by_oid:
            continue
        if rule.is_recovery and rule.recovery_of:
            target = by_rule_name.get(rule.recovery_of)
            if target is None:
                sys.exit(f"{rule.rule_name} recovers unknown rule "
                         f"{rule.recovery_of!r}")
            # Clear what the RAISE produced. Deriving the name from the
            # recovery rule instead is how a clear ends up naming an event type
            # nothing ever raises: it resolves nothing, the alarm stays open,
            # and the console shows a fault on equipment that recovered.
            cleared = event_type_of_rule(target)
            by_oid[rule.trap_oid].append({
                "event_type": cleared, "severity": "CLEAR", "is_clear": True,
                "clears": [cleared], "name": rule.rule_name, "vendor": "rule",
                "display_name": snake(rule.rule_name).replace("_", " ").capitalize(),
                "device_types": sorted(set(target.device_types or ())),
                "measures": None,
            })
            continue
        by_oid[rule.trap_oid].append({
            "event_type": event_type_of_rule(rule),
            "severity": SEVERITY_MAP.get(rule.severity, "MINOR"),
            "is_clear": False, "clears": [],
            "name": rule.rule_name, "vendor": "rule",
            "display_name": snake(rule.rule_name).replace("_", " ").capitalize(),
            "device_types": sorted(set(rule.device_types or ())),
            "measures": None,
        })

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
        "#",
        "# `device_types:` narrows a meaning to the equipment that can send it.",
        "# One wire OID often carries several conditions - a Liebert card sends",
        "# 476.1.42.3.3.0.5 for a fan failure and for a charger failure - and the",
        "# receiver tells them apart by the device the trap arrived from. An entry",
        "# with no device_types is the general meaning and applies to anything,",
        "# including a trap that could not be attributed to an endpoint.",
        "#",
        "# What device type CANNOT separate is marked AMBIGUOUS below: vendors",
        "# that put many conditions on ONE OID for ONE kind of device, with the",
        "# condition itself in a varbind. Liebert 476.1.42.3.3.0.1 carries seven.",
        "# Resolving those needs varbind-level disambiguation, which this file",
        "# has the shape for (`instance_from_varbind`) and the generator does not",
        "# yet emit - the ambiguity is recorded rather than hidden behind a",
        "# winner nobody chose deliberately.",
        "",
        "version: 1",
        "",
        "traps:",
    ]

    ambiguous = clears = 0
    for oid in sorted(by_oid, key=lambda o: [int(p) for p in o.split(".") if p.isdigit()]):
        entries = by_oid[oid]
        uniq = {(e["event_type"], e["severity"], e["is_clear"]): e for e in entries}
        for e in uniq.values():
            e.setdefault("match_varbind", None)
        vendors = sorted({e["vendor"] for e in entries})

        # EVERY meaning is emitted, each with the device types that can send
        # it, and the receiver picks by the sender. Collapsing a shared OID to
        # one winner - which this did, preferring clears - meant a Liebert fan
        # failure was read as a charger failure for as long as the table said
        # so: one of the two readings was always wrong, and nothing recorded
        # which.
        ordered = sorted(uniq.values(),
                         key=lambda e: (e["match_varbind"] is None,
                                        not e["device_types"], e["event_type"]))
        unresolvable = [e for e in ordered
                        if not e["device_types"] and e["match_varbind"] is None]
        if len(ordered) > 1 and len(unresolvable) > 1:
            # Two meanings that BOTH accept any device cannot be told apart by
            # the sender. Count it, keep the widest, and say so in the file
            # rather than pretending the ambiguity is gone.
            ambiguous += 1

        for e in ordered:
            lines.append(f"  - oid: {oid}")
            lines.append(f"    event_type: {e['event_type']}")
            lines.append(f"    severity: {e['severity']}")
            if e["is_clear"]:
                clears += 1
                lines.append("    is_clear: true")
                lines.append(f"    clears: [{', '.join(e['clears'])}]")
            if e.get("measures"):
                metric, value_vb, thresh_vb = e["measures"]
                lines.append(f"    metric: {metric}")
                lines.append(f"    value_varbind: {value_vb}")
                lines.append(f"    threshold_varbind: {thresh_vb}")
            if e.get("display_name"):
                lines.append(f"    display_name: \"{e['display_name']}\"")
            if e["device_types"]:
                lines.append(f"    device_types: [{', '.join(e['device_types'])}]")
            if e["match_varbind"]:
                lines.append("    match_varbinds:")
                for m in e["match_varbind"]:
                    lines.append(f"      - oid: {m['oid']}")
                    if "equals_int" in m:
                        lines.append(f"        equals_int: {m['equals_int']}")
                    else:
                        lines.append(f"        contains: \"{m['contains']}\"")
            lines.append(f"    # {e['name']} [{', '.join(vendors)}]")
            if len(ordered) > 1 and len(unresolvable) > 1 and not e["device_types"]:
                others = sorted(f"{o['name']}->{o['event_type']}"
                                for o in unresolvable if o is not e)
                lines.append("    # AMBIGUOUS - no device type separates this "
                             "from: " + "; ".join(others))
            lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(by_oid)} wire OIDs, {clears} clears, "
          f"{ambiguous} ambiguous, from {len(TRAP_DEFINITIONS)} trap definitions, "
          f"{len(VENDOR_TRAPS)} vendor tables and {len(DEFAULT_RULES)} rules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
