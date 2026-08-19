#!/usr/bin/env python3
"""Generate the Modbus register templates from the device's own maps.

Modbus has NO DISCOVERY. The register map IS the mapping: nothing on the wire
says what address 0x0020 means, what scale applies, or which way round a 32-bit
value is stored. A real integration therefore ships a TEMPLATE PER MODEL, and
so does this one.

Hand-writing seven templates of twenty-odd points each is how an integration
acquires a transposed address that nobody notices until the number it produces
becomes load-bearing. So the STRUCTURE - address, space, data type, scale, word
order - is generated from `core.modbus_register_map`, and only the METRIC
ASSIGNMENT is authored here, in POINT_METRICS. A point with no assignment is
emitted with an empty metric and listed on stderr, so nothing is dropped
quietly.

    python contracts/tools/gen_modbus_templates.py           # write
    python contracts/tools/gen_modbus_templates.py --check   # CI drift check

Re-run after any change to the simulator's maps. --check fails if the committed
file no longer matches them, which is the whole point: a template that has
silently drifted from the device decodes into plausible nonsense.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "contracts" / "mappings" / "modbus" / "templates.yaml"
DEFAULT_SIM = ROOT.parent / "DCIM" / "Datacenter_Network_Simulator"

# ---------------------------------------------------------------- assignments
#
# (point name) -> (metric, instance). Instance "" means none.
#
# The rule followed throughout: one key per physical quantity, with the phase or
# the SOURCE in the instance. A transfer switch measures the same volts on two
# sources and a UPS on its input and output; three keys for that would make the
# same quantity incomparable across devices that happen to name it differently.
POINT_METRICS: dict[str, tuple[str, str]] = {
    # --- volts, amps, hertz -------------------------------------------------
    "Voltage_LL_Avg":      ("voltage_ll", "AVG"),
    "Voltage_A_N":         ("voltage_ln", "A"),
    "Voltage_B_N":         ("voltage_ln", "B"),
    "Voltage_C_N":         ("voltage_ln", "C"),
    "Current_Avg":         ("current", "AVG"),
    "Current_A":           ("current", "A"),
    "Current_B":           ("current", "B"),
    "Current_C":           ("current", "C"),
    "Frequency":           ("line_frequency", ""),
    "Normal_Voltage":      ("voltage_ll", "NORMAL"),
    "Emergency_Voltage":   ("voltage_ll", "EMERGENCY"),
    "Active_Frequency":    ("line_frequency", "ACTIVE"),
    "Normal_Frequency":    ("line_frequency", "NORMAL"),
    "Emergency_Frequency": ("line_frequency", "EMERGENCY"),
    "Input_Voltage":       ("voltage_ll", "INPUT"),
    "Input_Frequency":     ("line_frequency", "INPUT"),

    # --- power --------------------------------------------------------------
    "Active_Power":        ("power_draw", ""),
    "Output_Power":        ("power_draw", "OUTPUT"),
    "Reactive_Power":      ("reactive_power", ""),
    "Apparent_Power":      ("apparent_power", ""),
    "Power_Factor":        ("power_factor", ""),
    "Load_Percent":        ("load_pct", ""),
    "Genset_Load_Percent": ("load_pct", ""),
    "Output_Load":         ("load_pct", "OUTPUT"),
    "Demand_Peak_kW":      ("demand_peak_power", ""),
    "Energy_Delivered":    ("energy_consumed", ""),

    # --- power quality ------------------------------------------------------
    "Voltage_Imbalance":   ("phase_imbalance_pct", ""),
    "THD_Voltage":         ("voltage_thd_pct", ""),
    "THD_Current":         ("current_thd_pct", ""),

    # --- generator / UPS / ATS ---------------------------------------------
    "Fuel_Level":          ("fuel_level_pct", ""),
    "Start_Attempts":      ("start_attempts", ""),
    "Total_Run_Hours":     ("run_hours", ""),
    "Current_Run_Minutes": ("current_run_time", ""),
    "Battery_Health":      ("battery_health_pct", ""),
    "Battery_Runtime":     ("battery_runtime", ""),
    "Transfer_Count":      ("transfer_count", ""),
    "Time_On_Emergency":   ("time_on_emergency", ""),

    # --- enumerated state words --------------------------------------------
    "Engine_State":        ("operating_mode", ""),
    "Switch_Position":     ("operating_mode", ""),
    "Operating_Mode":      ("operating_mode", ""),

}

# A transmitter's single measurement carries no meaning of its own. The metric
# comes from the probe ROLE - where the instrument is installed - so these
# points are marked rather than assigned. See PROBE_POINTS.
PROCESS_VALUE_POINTS = {"Process_Value", "Flow_Rate"}

# Discrete/coil bits. Status bits become equipment_state, faults become
# alarm_state - both with the bit's own name as the instance, so a key per
# vendor bit is never needed.
STATUS_BITS = {
    "Bus_Energized", "Breaker_Closed", "Tie_Closed", "Panel_Energized",
    "Source_Generator", "Source_Tie", "Service_Healthy", "Engine_Running",
    "Normal_Available", "Emergency_Available", "On_Emergency", "On_Battery",
    "Bypass_Active",
}

# The validity bit is NOT telemetry. It says whether the other registers mean
# anything yet, and it gates them - see the adapter.
VALIDITY_BITS = {"Data_Valid", "Reading_Valid"}

# Unit conversions from what the register carries to what the registry stores.
# The scale in the map turns raw words into the VENDOR's unit; this turns the
# vendor's unit into ours.
FACTORS: dict[str, float] = {
    "power_draw": 1000.0,          # kW -> W
    "reactive_power": 1000.0,      # kVAR -> VAR
    "apparent_power": 1000.0,      # kVA -> VA
    "demand_peak_power": 1000.0,   # kW -> W
    "battery_runtime": 60.0,       # minutes -> seconds
    "current_run_time": 60.0,      # minutes -> seconds
}

# Probe roles map to the loop they measure. The transmitter itself has one
# nameless "Process_Value"; what it MEANS comes from where it is installed,
# which is exactly how a field instrument works.
PROBE_POINTS: dict[str, tuple[str, str]] = {
    "chw_supply": ("water_supply_temp", "CHW"),
    "chw_return": ("water_return_temp", "CHW"),
    "cw_supply":  ("water_supply_temp", "COND"),
    "cw_return":  ("water_return_temp", "COND"),
    "ct_basin":   ("water_supply_temp", "BASIN"),
    "chw_flow":   ("water_flow", "CHW"),
}


def load_sim(sim_root: Path):
    if not (sim_root / "core" / "modbus_register_map.py").exists():
        sys.exit(f"simulator not found at {sim_root}; pass --sim-root")
    sys.path.insert(0, str(sim_root))
    import core.modbus_register_map as m  # noqa: E402
    return m


def yaml_str(s: str) -> str:
    return '"' + s.replace('"', '\\"') + '"'


def emit_point(m, p, space: str, unmapped: list[str], template: str) -> str:
    """One point as a YAML flow mapping."""
    fields = [f"space: {space}", f"addr: 0x{p.addr:04X}", f"name: {p.name}"]

    if space in ("input", "holding"):
        fields.append(f"dtype: {p.dtype}")
        if p.scale not in (1, 1.0):
            fields.append(f"scale: {p.scale:g}")

    if p.name in VALIDITY_BITS:
        fields.append("role: validity")
        return "      - { " + ", ".join(fields) + " }"

    if p.name in PROCESS_VALUE_POINTS:
        fields.append("role: process_value")
        return "      - { " + ", ".join(fields) + " }"

    if space in ("discrete", "coil"):
        metric = "equipment_state" if p.name in STATUS_BITS else "alarm_state"
        fields.append(f"metric: {metric}")
        fields.append(f"instance: {p.name}")
        return "      - { " + ", ".join(fields) + " }"

    assigned = POINT_METRICS.get(p.name)
    if assigned is None:
        unmapped.append(f"{template}/{p.name}")
        fields.append('metric: ""')
        return "      - { " + ", ".join(fields) + " }"

    metric, instance = assigned
    fields.append(f"metric: {metric}")
    if instance:
        fields.append(f"instance: {instance}")
    if metric in FACTORS:
        fields.append(f"factor: {FACTORS[metric]:g}")
    if p.enum:
        pairs = ", ".join(f"{v}: {yaml_str(k)}" for k, v in sorted(p.enum.items(),
                                                                  key=lambda kv: kv[1]))
        fields.append("enum: { " + pairs + " }")
    return "      - { " + ", ".join(fields) + " }"


def build(m) -> tuple[str, list[str]]:
    unmapped: list[str] = []
    out: list[str] = []
    a = out.append

    a("# Modbus register templates. GENERATED - edit gen_modbus_templates.py.")
    a("#")
    a("# Modbus has no discovery. Nothing on the wire says what address 0x0020")
    a("# means, what scale applies, or which way round a 32-bit value is stored,")
    a("# so a template per model is the entire integration - exactly as it is on")
    a("# a real site.")
    a("#")
    a("# Keyed by MAP ID, which FC43 (Read Device Identification) serves back, so")
    a("# the adapter can check at first contact that the template it is about to")
    a("# decode with belongs to the device answering. Pointing the wrong template")
    a("# at a device does not fail: it returns numbers.")
    a("#")
    a("# scale  divides the raw register to give the VENDOR's unit")
    a("# factor multiplies the vendor's unit to give the REGISTRY's unit")
    a("#")
    a("# word_order is per template because it is per vendor. Decoding an Eaton")
    a("# map with Schneider word order yields energy off by a factor of 65536,")
    a("# which is the most common Modbus integration bug there is.")
    a("version: 1")
    a("")
    a("templates:")

    for dtype_name, mm in sorted(m.MODBUS_MAPS.items()):
        a("")
        a(f"  {mm.map_id}:")
        a(f"    vendor: {yaml_str(mm.vendor)}")
        a(f"    product: {yaml_str(mm.product)}")
        a(f"    word_order: {mm.word_order}")
        a(f"    device_types: [{dtype_name}]")
        a("    points:")
        for space_const, space in ((m.SPACE_INPUT, "input"),
                                   (m.SPACE_HOLDING, "holding"),
                                   (m.SPACE_DISCRETE, "discrete"),
                                   (m.SPACE_COIL, "coil")):
            for p in mm.points.get(space_const, []):
                a(emit_point(m, p, space, unmapped, mm.map_id))

    # Field transmitters. One template serves several roles, and the role is
    # what says which loop the reading belongs to.
    seen: dict[str, object] = {}
    for role, mm in m.PROBE_MAPS.items():
        seen.setdefault(mm.map_id, mm)

    for map_id, mm in sorted(seen.items()):
        roles = sorted(r for r, x in m.PROBE_MAPS.items() if x.map_id == map_id)
        a("")
        a(f"  {map_id}:")
        a(f"    vendor: {yaml_str(mm.vendor)}")
        a(f"    product: {yaml_str(mm.product)}")
        a(f"    word_order: {mm.word_order}")
        a("    device_types: [sensor]")
        a("    # A transmitter publishes one nameless process value; what it MEANS")
        a("    # comes from where it is installed. The probe role carried on the")
        a("    # endpoint selects the metric, which is how a field instrument")
        a("    # actually works.")
        a("    probe_roles:")
        for role in roles:
            metric, instance = PROBE_POINTS[role]
            line = f"      {role}: {{ metric: {metric}"
            if instance:
                line += f", instance: {instance}"
            a(line + " }")
        a("    points:")
        for space_const, space in ((m.SPACE_INPUT, "input"),
                                   (m.SPACE_HOLDING, "holding"),
                                   (m.SPACE_DISCRETE, "discrete"),
                                   (m.SPACE_COIL, "coil")):
            for p in mm.points.get(space_const, []):
                a(emit_point(m, p, space, unmapped, map_id))

    return "\n".join(out) + "\n", unmapped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sim-root", default=str(DEFAULT_SIM))
    ap.add_argument("--check", action="store_true",
                    help="fail if the committed file differs from the maps")
    args = ap.parse_args()

    m = load_sim(Path(args.sim_root).resolve())
    text, unmapped = build(m)

    if args.check:
        if not OUT.exists():
            print(f"{OUT} does not exist", file=sys.stderr)
            return 1
        if OUT.read_text(encoding="utf-8") != text:
            print("modbus templates are stale; run "
                  "`python contracts/tools/gen_modbus_templates.py`", file=sys.stderr)
            return 1
        print("modbus templates are up to date")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    n = text.count("- { space:")
    print(f"wrote {OUT.relative_to(ROOT)}: {n} points")
    if unmapped:
        print(f"\n{len(unmapped)} point(s) with no metric assignment - they are "
              f"emitted with an empty metric and will not be polled:", file=sys.stderr)
        for u in unmapped:
            print(f"  {u}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
