import type { ChartGroup } from './DeviceHistory';

/** Which charts a device type gets, and in what order.
 *
 *  Every panel is ONE unit, so every frame has one axis. Two scales on a shared
 *  frame let any two series be made to look correlated by choosing the ranges,
 *  which is the most common way a chart lies.
 *
 *  Without an entry here a device falls back to grouping by unit, which is a
 *  reasonable default and not a layout: it will put a fan tachometer beside a
 *  valve position because both happen to be percentages, and it captions the
 *  panel with whatever metric keys came back.
 *
 *  Every list below was checked against what the plane actually reports for that
 *  type, not against what the type sounds like it should have. A panel naming a
 *  metric nobody sends is worse than no panel: it renders as "not reported" on
 *  every device forever.
 *
 *  PER-INTERFACE METRICS ARE DELIBERATELY ABSENT. if_in_bps and friends carry an
 *  instance per port, and a switch here has 129 of them - one chart, 129 lines.
 *  Interface traffic needs a port picker, which is a different feature; drawing
 *  it as a hairball would be worse than leaving it out.
 */

/** Control plane and thermals - the same silicon story on every NOS box. */
const NETWORK: ChartGroup[] = [
  { title: 'Utilisation', metrics: ['cpu_utilization', 'memory_utilization'] },
  { title: 'Temperature', metrics: ['cpu_temperature', 'component_temperature'] },
];

const SERVER: ChartGroup[] = [
  { title: 'Power draw', metrics: ['power_draw'] },
  // Intake is what the room delivers and the CPU is what the machine does with
  // it, so the two belong on one frame: the GAP between them is the reading.
  { title: 'CPU and intake temperature',
    metrics: ['cpu_temperature', 'inlet_temperature'] },
  { title: 'Utilisation',
    metrics: ['cpu_utilization', 'memory_utilization', 'disk_utilization'] },
];

/** A strip's own question is how full it is; the amps and volts behind that are
 *  what an electrician asks next. */
const PDU: ChartGroup[] = [
  { title: 'Load', metrics: ['load_pct'] },
  { title: 'Power draw', metrics: ['power_draw'] },
  { title: 'Current', metrics: ['current'] },
  { title: 'Input voltage', metrics: ['voltage_ln'] },
  { title: 'Intake air', metrics: ['ambient_temperature'] },
];

const UPS: ChartGroup[] = [
  // Load and battery health share a scale and are read together: a healthy
  // battery at 90% load and a failing one at 30% are different emergencies.
  { title: 'Load and battery health',
    metrics: ['load_pct', 'battery_health_pct'] },
  { title: 'Runtime remaining', metrics: ['battery_runtime'] },
  { title: 'Power draw', metrics: ['power_draw'] },
  { title: 'Output voltage', metrics: ['voltage_ll'] },
];

/** Supply, return and setpoint on one frame is the whole cooling story: the
 *  spread is the work done, and the distance from setpoint is whether the unit
 *  is keeping up. */
const CRAH: ChartGroup[] = [
  { title: 'Air temperatures',
    metrics: ['supply_air_temp', 'return_air_temp', 'air_setpoint_temp'] },
  { title: 'Output and airflow',
    metrics: ['cooling_output_pct', 'fan_speed_pct', 'valve_position_pct'] },
  { title: 'Return humidity', metrics: ['relative_humidity'] },
  { title: 'Power draw', metrics: ['power_draw'] },
];

const CDU: ChartGroup[] = [
  { title: 'Water temperatures',
    metrics: ['water_supply_temp', 'water_return_temp', 'water_setpoint_temp'] },
  { title: 'Pump and valve',
    metrics: ['pump_speed_pct', 'valve_position_pct'] },
  { title: 'Water flow', metrics: ['water_flow'] },
  { title: 'Thermal load', metrics: ['thermal_load', 'power_draw'] },
  // Filter differential is how a filter tells you it is blocking, and it shares
  // its unit with loop pressure without sharing its meaning - hence two panels.
  { title: 'Water pressure', metrics: ['water_pressure'] },
  { title: 'Filter differential', metrics: ['filter_diff_pressure'] },
];

const CHILLER: ChartGroup[] = [
  { title: 'Water temperatures',
    metrics: ['water_supply_temp', 'water_return_temp', 'water_setpoint_temp'] },
  { title: 'Compressor load', metrics: ['compressor_load_pct'] },
  { title: 'Capacity and power', metrics: ['cooling_capacity', 'power_draw'] },
  { title: 'Water flow', metrics: ['water_flow'] },
];

const COOLING_TOWER: ChartGroup[] = [
  { title: 'Water temperatures',
    metrics: ['water_supply_temp', 'water_return_temp'] },
  // Wet bulb is the floor a tower can reach; dry bulb alone does not explain a
  // tower that has stopped keeping up on a humid day.
  { title: 'Outdoor air',
    metrics: ['outdoor_wet_bulb_temp', 'outdoor_dry_bulb_temp'] },
  { title: 'Fan and basin', metrics: ['fan_speed_pct', 'basin_level_pct'] },
  { title: 'Power draw', metrics: ['power_draw'] },
];

const PUMP: ChartGroup[] = [
  { title: 'Speed', metrics: ['pump_speed_pct'] },
  { title: 'Water flow', metrics: ['water_flow'] },
  { title: 'Pressure', metrics: ['water_pressure', 'water_diff_pressure'] },
  { title: 'Motor temperature', metrics: ['motor_temp'] },
  { title: 'Power draw', metrics: ['power_draw'] },
];

const GENERATOR: ChartGroup[] = [
  { title: 'Load and fuel', metrics: ['load_pct', 'fuel_level_pct'] },
  { title: 'Power draw', metrics: ['power_draw'] },
];

const ATS: ChartGroup[] = [
  { title: 'Source voltage', metrics: ['voltage_ll'] },
  { title: 'Line frequency', metrics: ['line_frequency'] },
];

export const CHART_GROUPS: Record<string, ChartGroup[]> = {
  server: SERVER,
  router: NETWORK,
  switch: NETWORK,
  oob_switch: NETWORK,
  firewall: NETWORK,
  load_balancer: NETWORK,
  pdu: PDU,
  floor_pdu: PDU,
  ups: UPS,
  crah: CRAH,
  crac: CRAH,
  cdu: CDU,
  chiller: CHILLER,
  cooling_tower: COOLING_TOWER,
  pump: PUMP,
  generator: GENERATOR,
  ats: ATS,
};

/** The panels for a device type, or undefined to fall back to unit grouping. */
export function chartGroupsFor(deviceType: string): ChartGroup[] | undefined {
  return CHART_GROUPS[deviceType];
}
