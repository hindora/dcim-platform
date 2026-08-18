# 06 — Canonical Metric Registry

The registry is the dictionary that makes "canonical telemetry" mean something.
It lives at `contracts/metrics/registry.yaml`, is loaded into the `metric` table
at migration time, and is code-generated into Go constants, Python enums and
TypeScript union types. A metric key that is not in the registry is a **hard
validation failure** in the collector, not a silently-passed string.

---

## 1. Naming rules

1. `snake_case`, ASCII, no protocol names in the key. `cpu_temperature`, never
   `redfish_cpu_temp`.
2. The key names **what is measured**, never how it was read or where it lives.
   `inlet_temperature` is right; `crah_supply_temp_bacnet_ai_1` is wrong twice.
3. **One unit per key, forever.** Temperature is always °C, power always W,
   energy always kWh, pressure always kPa, flow always L/s, percentages always
   0–100. If a device reports °F or GPM, the *adapter* converts. Changing a
   registered unit is a v2 break (see `05-telemetry-contract.md` §6).
4. Multi-instance metrics use `instance`, not a key suffix. Per-interface
   throughput is `if_in_octets` with `instance = "1"`, never `if1_in_octets`.
5. Counters and their derived rates are **separate keys**:
   `if_in_octets` (counter) and `if_in_bps` (gauge).
6. Binary/status points end in `_state` (`chiller_running_state`) or `_alarm`
   (`leak_alarm`) and are `bool`.

---

## 2. Registry entry schema

```yaml
- key: cpu_temperature
  display_name: CPU Temperature
  unit: C
  value_type: gauge         # gauge|counter|delta|bool|text
  aggregation: avg          # avg|sum|max|min|last  — how CAGGs roll it up
  min_valid: -20
  max_valid: 130
  stale_after_s: 120
  hot: true                 # mirrored into device_state.metrics for the rack view
  device_types: [server]
  group: thermal            # used by poll_profile.metric_selectors
  description: Package temperature of a CPU socket. instance = socket id.
```

`aggregation` matters and is routinely got wrong: rolling `power_w` up across a
rack is `sum`, but rolling the *same metric* up over *time* is `avg`. The
registry field is the **time** aggregation; spatial aggregation is decided by the
analytics layer per query.

---

## 3. The registry (v1)

Only the metric keys are normative. The "typical source" column is guidance for
adapter authors; several metrics are legitimately available from more than one
protocol, and the poll profile decides which endpoint actually supplies it.

### 3.1 Universal / device-level

| key | unit | type | agg | hot | typical source |
|---|---|---|---|---|---|
| `sys_uptime` | s | counter | last | | SNMP `sysUpTime` (1.3.6.1.2.1.1.3), Redfish |
| `reachable` | bool | bool | last | ✓ | derived from endpoint state |
| `device_health` | text | text | last | ✓ | Redfish `Status.Health`, vendor MIB |
| `poll_latency` | ms | gauge | avg | | collector-measured |

### 3.2 Compute (server)

| key | unit | type | agg | hot | typical source |
|---|---|---|---|---|---|
| `cpu_utilization` | pct | gauge | avg | ✓ | SNMP HOST-RESOURCES `hrProcessorLoad` (…25.3.3.1.2), UCD (…2021), Redfish |
| `cpu_temperature` | C | gauge | max | ✓ | Redfish `/Thermal#/Temperatures`, BMC MIB (sim: 1.3.6.1.4.1.99999.26) |
| `memory_utilization` | pct | gauge | avg | ✓ | HOST-RESOURCES `hrStorage` (…25.2.1), UCD |
| `memory_used` | B | gauge | avg | | HOST-RESOURCES |
| `memory_total` | B | gauge | last | | HOST-RESOURCES |
| `disk_utilization` | pct | gauge | avg | | HOST-RESOURCES `hrStorage` |
| `disk_used` / `disk_total` | B | gauge | avg/last | | HOST-RESOURCES |
| `inlet_temperature` | C | gauge | max | ✓ | Redfish `/Thermal` "Inlet", ENTITY-SENSOR (…99.1.1.1) |
| `exhaust_temperature` | C | gauge | max | | Redfish `/Thermal` |
| `fan_speed` | rpm | gauge | avg | | Redfish `/Thermal#/Fans`; instance = fan id |
| `fan_speed_pct` | pct | gauge | avg | | Redfish |
| `power_draw` | W | gauge | avg | ✓ | Redfish `/Power#/PowerControl`, PDU per-outlet |
| `psu_input_voltage` | V | gauge | avg | | Redfish `/Power#/PowerSupplies`; instance = PSU |
| `psu_output_power` | W | gauge | avg | | Redfish |
| `psu_state` | bool | bool | last | | Redfish `PowerSupplies[].Status.State` |
| `energy_consumed` | kWh | counter | last | | Redfish, PDU |

### 3.3 Network

| key | unit | type | agg | hot | typical source |
|---|---|---|---|---|---|
| `if_admin_state` | bool | bool | last | | IF-MIB `ifAdminStatus`, gNMI |
| `if_oper_state` | bool | bool | last | ✓ | IF-MIB `ifOperStatus`, gNMI, linkUp/linkDown traps |
| `if_in_octets` / `if_out_octets` | B | counter | last | | IF-MIB `ifHCIn/OutOctets` (…31.1.1.1), gNMI counters |
| `if_in_bps` / `if_out_bps` | bps | gauge | avg | ✓ | derived at ingest |
| `if_in_errors` / `if_out_errors` | count | counter | last | | IF-MIB, gNMI |
| `if_in_discards` / `if_out_discards` | count | counter | last | | IF-MIB, gNMI |
| `if_utilization` | pct | gauge | max | | derived from bps ÷ `ifHighSpeed` |
| `if_speed` | bps | gauge | last | | IF-MIB `ifHighSpeed`, gNMI |
| `bgp_session_state` | bool | bool | last | | gNMI network-instances, BGP traps |
| `lldp_neighbor_count` | count | gauge | last | | LLDP-MIB, gNMI `openconfig-lldp` |

All `instance` values here are the **ifIndex as a string**, and the interface
name is resolved by the ingest worker from the `interface` table. Do not key on
name — names change, ifIndex is what the counters carry.

### 3.4 Power — UPS

| key | unit | type | agg | hot | typical source |
|---|---|---|---|---|---|
| `ups_input_voltage` | V | gauge | avg | | UPS-MIB `upsInput` (1.3.6.1.2.1.33.1.3) |
| `ups_output_voltage` | V | gauge | avg | | UPS-MIB `upsOutput` |
| `ups_output_current` | A | gauge | avg | | UPS-MIB |
| `ups_output_power` | W | gauge | avg | ✓ | UPS-MIB `upsOutputPower` |
| `ups_load_pct` | pct | gauge | max | ✓ | UPS-MIB `upsOutputPercentLoad` |
| `ups_battery_charge` | pct | gauge | min | ✓ | UPS-MIB `upsEstimatedChargeRemaining` |
| `ups_battery_runtime` | min | gauge | min | ✓ | UPS-MIB `upsEstimatedMinutesRemaining` |
| `ups_battery_voltage` | V | gauge | avg | | UPS-MIB |
| `ups_battery_temperature` | C | gauge | max | | UPS-MIB `upsBatteryTemperature` |
| `ups_on_battery_state` | bool | bool | last | ✓ | UPS-MIB `upsOutputSource`, `upsOnBattery` trap |
| `ups_bypass_state` | bool | bool | last | | UPS-MIB |
| `ups_input_frequency` | Hz | gauge | avg | | UPS-MIB |

### 3.5 Power — PDU / RPP / floor PDU

| key | unit | type | agg | hot | typical source |
|---|---|---|---|---|---|
| `pdu_input_voltage` | V | gauge | avg | | vendor MIB (APC 318, Raritan 13742); sim enterprise 1.3.6.1.4.1.99999.5 |
| `pdu_input_current` | A | gauge | max | ✓ | vendor MIB; instance = phase A/B/C |
| `pdu_active_power` | W | gauge | avg | ✓ | vendor MIB |
| `pdu_apparent_power` | VA | gauge | avg | | vendor MIB |
| `pdu_power_factor` | ratio | gauge | avg | | vendor MIB |
| `pdu_energy` | kWh | counter | last | | vendor MIB |
| `pdu_load_pct` | pct | gauge | max | ✓ | derived vs rated |
| `pdu_bank_current` | A | gauge | max | | vendor MIB; instance = bank |
| `outlet_current` | A | gauge | max | | vendor MIB; instance = outlet number |
| `outlet_active_power` | W | gauge | avg | | vendor MIB |
| `outlet_state` | bool | bool | last | | vendor MIB (switched PDUs only) |

### 3.6 Power — utility / generator / switchgear / ATS

| key | unit | type | agg | hot | typical source |
|---|---|---|---|---|---|
| `feed_voltage_ll` | V | gauge | avg | | Modbus (revenue meter); sim SNMP 1.3.6.1.4.1.99999.8 |
| `feed_current` | A | gauge | max | | Modbus; instance = phase |
| `feed_active_power` | W | gauge | avg | ✓ | Modbus |
| `feed_energy` | kWh | counter | last | ✓ | Modbus — **the PUE numerator source** |
| `feed_frequency` | Hz | gauge | avg | | Modbus |
| `feed_power_factor` | ratio | gauge | avg | | Modbus |
| `generator_running_state` | bool | bool | last | ✓ | SNMP (sim 1.3.6.1.4.1.99999.7) |
| `generator_output_power` | W | gauge | avg | | SNMP |
| `generator_fuel_level` | pct | gauge | min | ✓ | SNMP |
| `generator_run_hours` | h | counter | last | | SNMP |
| `generator_coolant_temperature` | C | gauge | max | | SNMP |
| `generator_battery_voltage` | V | gauge | min | | SNMP |
| `ats_source_selected` | text | text | last | ✓ | SNMP (sim 1.3.6.1.4.1.99999.10) |
| `ats_transfer_count` | count | counter | last | | SNMP |
| `breaker_state` | bool | bool | last | | SNMP/Modbus; instance = breaker id |
| `switchgear_bus_voltage` | V | gauge | avg | | SNMP (sim 1.3.6.1.4.1.99999.9) / Modbus |

### 3.7 Cooling — chiller

Keys map 1:1 onto the simulator's BACnet analog inputs
(`core/bacnet_plant_generator.py`, `PLANT_SPEC["chiller"]`):

| key | unit | type | agg | hot | BACnet object name |
|---|---|---|---|---|---|
| `chws_temperature` | C | gauge | avg | ✓ | `CHW_Supply_Temp` |
| `chwr_temperature` | C | gauge | avg | | `CHW_Return_Temp` |
| `chws_setpoint` | C | gauge | last | | `CHW_Setpoint` |
| `chw_flow` | lps | gauge | avg | | `CHW_Flow` |
| `cws_temperature` | C | gauge | avg | | `Cond_Supply_Temp` |
| `cwr_temperature` | C | gauge | avg | | `Cond_Return_Temp` |
| `compressor_load` | pct | gauge | avg | ✓ | `Compressor_Load` |
| `active_power` | W | gauge | avg | ✓ | `Active_Power` (kW on the wire → ×1000) |
| `cooling_capacity` | W | gauge | last | | `Cooling_Capacity` |
| `cop` | ratio | gauge | avg | ✓ | `COP` |
| `evaporator_pressure` | kPa | gauge | avg | | `Evap_Pressure` |
| `condenser_pressure` | kPa | gauge | avg | | `Cond_Pressure` |
| `run_hours` | h | counter | last | | `Run_Hours` |
| `chiller_running_state` | bool | bool | last | ✓ | BI `Chiller_Running` |
| `high_pressure_alarm` | bool | bool | last | | BI `Alarm_HighPressure` |
| `low_evap_temp_alarm` | bool | bool | last | | BI `Alarm_LowEvapTemp` |
| `flow_loss_alarm` | bool | bool | last | | BI `Alarm_FlowLoss` |
| `high_chws_alarm` | bool | bool | last | | BI `Alarm_HighCHWSupply` |
| `cond_press_limit_alarm` | bool | bool | last | | BI `Alarm_CondPressLimit` |

The simulator's own source notes that `Alarm_HighPressure` is the **cutout**
(latched, machine off, manual reset) while `Alarm_CondPressLimit` is the
**capacity limit** (machine running and unloading itself). Keep them as distinct
metrics and distinct alarm types — collapsing them is exactly the mistake the
simulator's comment records having caused.

### 3.8 Cooling — pump / tower / valve / CDU / CRAH

| key | unit | type | agg | source object |
|---|---|---|---|---|
| `pump_speed_pct` | pct | gauge | avg | pump `Speed` |
| `pump_flow` | lps | gauge | avg | pump `Flow` |
| `discharge_pressure` / `suction_pressure` / `differential_pressure` | kPa | gauge | avg | pump |
| `motor_power` | W | gauge | avg | pump `Motor_Power` |
| `motor_temperature` | C | gauge | max | pump `Motor_Temp` |
| `vfd_frequency` | Hz | gauge | avg | pump `VFD_Frequency` |
| `pump_running_state` / `pump_fault_alarm` / `low_flow_alarm` | bool | bool | last | pump BI |
| `tower_fan_speed_pct` | pct | gauge | avg | tower `Fan_Speed` |
| `basin_temperature` | C | gauge | avg | tower `Basin_Temp` |
| `cond_water_in_temperature` / `cond_water_out_temperature` | C | gauge | avg | tower |
| `fan_power` | W | gauge | avg | tower / CRAH `Fan_Power` |
| `basin_level_pct` | pct | gauge | min | tower `Basin_Level` |
| `makeup_flow` | lpm | gauge | avg | tower `Makeup_Flow` |
| `vibration` | mm_s | gauge | max | tower `Vibration` |
| `tower_fan_state` / `high_vibration_alarm` / `low_basin_alarm` | bool | bool | last | tower BI |
| `valve_position_pct` | pct | gauge | avg | valve `Position` |
| `valve_commanded_position_pct` | pct | gauge | last | valve `Commanded_Position` |
| `actuator_temperature` | C | gauge | max | valve `Actuator_Temp` |
| `valve_modulating_state` / `actuator_fault_alarm` | bool | bool | last | valve BI |
| `tcs_supply_temperature` / `tcs_return_temperature` / `tcs_setpoint` | C | gauge | avg | CDU |
| `tcs_flow` | lps | gauge | avg | CDU `TCS_Flow` |
| `facility_chw_valve_pct` | pct | gauge | avg | CDU |
| `facility_chw_flow` | lps | gauge | avg | CDU |
| `tcs_loop_pressure` | kPa | gauge | avg | CDU |
| `heat_load` | W | gauge | avg | CDU `Heat_Load` |
| `pump_power` | W | gauge | avg | CDU `Pump_Power` |
| `approach_temperature` | C | gauge | avg | CDU `Approach_Temp` |
| `filter_dp` | kPa | gauge | max | CDU `Filter_DP` |
| `cdu_running_state` / `leak_alarm` / `high_supply_temp_alarm` / `pump_fault_alarm` / `low_flow_alarm` | bool | bool | last | CDU BI |
| `supply_air_temperature` / `return_air_temperature` / `air_setpoint` | C | gauge | avg | CRAH |
| `crah_fan_speed_pct` | pct | gauge | avg | CRAH `Fan_Speed` |
| `chw_valve_pct` | pct | gauge | avg | CRAH `CHW_Valve` |
| `cooling_capacity_pct` | pct | gauge | avg | CRAH `Cooling_Capacity` |
| `supply_humidity` | pct | gauge | avg | CRAH `Supply_Humidity` |
| `airflow_pct` | pct | gauge | avg | CRAH `Airflow` |
| `crah_running_state` / `high_temp_alarm` / `airflow_loss_alarm` / `filter_dirty_alarm` / `high_return_air_alarm` | bool | bool | last | CRAH BI |

Note that `Alarm_HighTemp` (discharge — the unit has failed) and
`Alarm_HighReturnAir` (the hot aisle feeding it is too hot — the unit is fine)
are different conditions with different owners. Keep them distinct, as the
simulator does.

### 3.9 Environment

| key | unit | type | agg | hot | typical source |
|---|---|---|---|---|---|
| `ambient_temperature` | C | gauge | max | ✓ | ENTITY-SENSOR-MIB (1.3.6.1.2.1.99.1.1.1), Raritan (13742.6.5.5.3.1), Geist (21239.5.1), APC NetBotz (318.1.1.10.4.2.2.1) |
| `relative_humidity` | pct | gauge | avg | ✓ | same sensor families |
| `dew_point` | C | gauge | avg | | same |
| `airflow` | pct | gauge | avg | | same |
| `water_leak_state` | bool | bool | last | ✓ | leak detection sensors |
| `door_state` | bool | bool | last | | contact sensors |

Rack-mounted probes (Raritan DPX2 on a PX2 sensor port) have **no IP and no
endpoint of their own**: they are read through the PDU's agent. Model them as
devices whose only endpoint is `role = field_device` with
`via_endpoint_id = <the PDU's SNMP endpoint>`, and set `instance` to the sensor
port. This is exactly how a real DPX2 works.

### 3.10 Derived / platform metrics

Computed by the analytics layer, written back as telemetry so they chart and
alarm like anything else. `device_id` is the datacenter/room proxy device.

| key | unit | type | agg | notes |
|---|---|---|---|---|
| `pue` | ratio | gauge | avg | energy-based over the bucket, **not** instantaneous power |
| `it_load` | W | gauge | avg | Σ IT `power_draw` |
| `facility_load` | W | gauge | avg | Σ cooling + losses |
| `cooling_load` | W | gauge | avg | Σ plant `active_power` + `fan_power` + `motor_power` |
| `rack_load` | W | gauge | avg | Σ per-rack `power_draw`; instance = rack id |
| `rack_load_pct` | pct | gauge | max | vs `rack.rated_power_kw` |
| `room_delta_t` | C | gauge | avg | mean return − mean supply |
| `capacity_headroom_kw` | W | gauge | min | per rack / room / DC |

---

## 4. Code generation

`make -C contracts metrics` produces:

- `collector/pkg/models/metrics_gen.go` — `const MetricCPUTemperature = "cpu_temperature"`
  plus a `map[string]MetricDef` for range/unit validation at emit time;
- `backend/app/core/metrics_gen.py` — a `StrEnum` plus a Pydantic-validated
  `METRICS: dict[str, MetricDef]`;
- `frontend/src/lib/metrics.gen.ts` — a union type plus unit/format metadata so
  the UI never hardcodes "°C".

CI fails if generated files differ from a fresh generation, and `mapcheck` fails
if any mapping YAML references a key not in the registry.

---

## 5. Adding a metric

1. Add the entry to `registry.yaml`.
2. Regenerate; commit generated files.
3. Add an Alembic migration that upserts the `metric` row (never delete —
   deprecate with `deprecated_at`, because hypertable rows still point at the id).
4. Add the protocol mapping in `contracts/mappings/<proto>/`.
5. Add an adapter test asserting the metric appears with the right unit against
   the running simulator.

No collector code change is required for steps 1–4 unless a new *transform* is
needed. That is the point of keeping mappings as data.
