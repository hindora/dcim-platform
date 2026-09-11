/** What stands in one room, grouped by what kind of machine it is.
 *
 *  One table with one column set could not do this. A facility room mixes a
 *  chiller with a pump, a valve, a thermowell, a meter and the gateway they
 *  all talk through, and those have almost nothing in common to put in a
 *  column: a BACnet router read as a row of dashes, a meter's throughput had
 *  nowhere to go at all, and a valve's commanded-against-measured - the one
 *  number that says whether its actuator is obeying - was invisible.
 *
 *  So each family gets its own table and its own columns. The cost is more
 *  headings; the return is that every column in every table means something
 *  for every row under it, which is the same rule the facility table itself
 *  was built on.
 *
 *  Order follows a walk round the room rather than the alphabet: the machines
 *  that move heat, then the ones that move the water, then what measures it,
 *  then the electrical spine, then the boxes that carry the signals.
 */

import { type PlantMachine } from '../../api/client';
import { Column, DataTable, Num } from '../../components/estate';
import { Tip } from '../../components/HoverTip';
import { TYPE_WORD, Verdict, verdictTone } from './Plant';

type Unit = 'c' | 'f';

const conv = (c: number | null | undefined, unit: Unit) =>
  c === null || c === undefined ? null : unit === 'c' ? c : c * 9 / 5 + 32;
const convDelta = (c: number | null | undefined, unit: Unit) =>
  c === null || c === undefined ? null : unit === 'c' ? c : c * 9 / 5;

/** A dash that says WHY it is a dash. The whole point of splitting these
 *  tables is that an empty cell should be rare and meaningful. */
function Dash({ why }: { why: string }) {
  return <Tip className="dash" tip={why}>—</Tip>;
}

function num(v: number | null | undefined, digits: number, unit?: string,
             why = 'not reported') {
  if (v === null || v === undefined) return <Dash why={why} />;
  return <Num value={v} digits={digits} unit={unit} />;
}

/** The per-kind word for what a machine is doing, which is never "running"
 *  for half of this equipment: a board is energised, a transfer switch is on
 *  a source, a UPS is on mains. */
function State({ m }: { m: PlantMachine }) {
  const label = m.state_label
    ?? (m.running === null || m.running === undefined
      ? null : m.running ? 'Running' : 'Off');
  if (!label) return <Dash why="this device publishes no run state" />;
  const quiet = label === 'Off' || label === 'Standby' || label === 'Dead';
  const tip = Object.entries(m.states ?? {}).length
    ? <>{Object.entries(m.states ?? {}).map(([k, v]) => (
        <span className="spread-line" key={k}>
          {k.replace(/_/g, ' ')}: <b>{v ? 'yes' : 'no'}</b></span>))}</>
    : 'from the machine\'s own state binary';
  return <Tip tip={tip}><span className={quiet ? 'muted' : undefined}>{label}</span></Tip>;
}

const NAME: Column<PlantMachine> = {
  key: 'name', label: 'Machine', width: 200,
  help: 'The machine, as inventory names it.',
  sort: (m) => m.name,
  render: (m) => <b>{m.name}</b>,
};

const ALARMS: Column<PlantMachine> = {
  key: 'alarms', label: 'Alarms', align: 'num', width: 78,
  help: 'Open conditions on this machine.',
  sort: (m) => m.alarms_open,
  render: (m) => (m.alarms_open
    ? <span className="warn">{m.alarms_open}</span>
    : <span className="muted">0</span>),
};

const VERDICT: Column<PlantMachine> = {
  key: 'verdict', label: 'Verdict', align: 'mid', width: 130,
  help: 'This machine’s state, judged from its own telemetry.',
  sort: (m) => m.verdict,
  render: (m) => <Verdict v={m.verdict} why={m.why} />,
};

const STATE: Column<PlantMachine> = {
  key: 'state', label: 'State', align: 'mid', width: 100,
  help: 'From the machine’s own binary, never guessed from watts.',
  sort: (m) => m.state_label ?? (m.running ? 'a' : 'b'),
  render: (m) => <State m={m} />,
};

function cooling(unit: Unit): Column<PlantMachine>[] {
  const u = unit === 'c' ? '°C' : '°F';
  return [
    NAME,
    { key: 'kind', label: 'Kind', width: 120, sort: (m) => m.device_type,
      help: 'What the machine is.',
      render: (m) => <span className="muted">{TYPE_WORD[m.device_type] ?? m.device_type}</span> },
    STATE,
    { key: 'duty', label: 'Duty', align: 'num', width: 80,
      help: 'Share of its rating it is delivering.',
      sort: (m) => m.duty_pct ?? null,
      render: (m) => (m.duty_pct === null || m.duty_pct === undefined
        ? <Dash why="publishes no duty figure" />
        : <Tip tip={`${m.duty_pct.toFixed(0)} % of ${m.duty_of}`}>
            <span>{m.duty_pct.toFixed(0)} %</span></Tip>) },
    { key: 'heat', label: 'Heat', align: 'num', width: 90,
      help: 'Heat it is moving right now.',
      sort: (m) => m.heat_kw,
      render: (m) => num(m.heat_kw, m.heat_kw && m.heat_kw >= 100 ? 0 : 1, 'kW',
                         'this machine does not measure the heat it moves') },
    { key: 'supply', label: `Supply ${u}`, align: 'num', width: 92,
      help: 'What it sends out — cold water, or cold air.',
      sort: (m) => m.supply_c,
      render: (m) => num(conv(m.supply_c, unit), 1, undefined, 'no supply sensor') },
    { key: 'return', label: `Return ${u}`, align: 'num', width: 92,
      help: 'What comes back to it, carrying the heat it picked up.',
      sort: (m) => m.return_c,
      render: (m) => num(conv(m.return_c, unit), 1, undefined, 'no return sensor') },
    { key: 'delta', label: `ΔT ${unit === 'c' ? 'K' : '°F'}`, align: 'num', width: 80,
      help: 'Return minus supply. Low on a moving loop is flow without transfer.',
      sort: (m) => m.delta_t_k,
      render: (m) => num(convDelta(m.delta_t_k, unit), 1, undefined,
                         'needs both ends of the loop') },
    { key: 'power', label: 'Power', align: 'num', width: 84,
      help: 'Electrical input to this machine.',
      sort: (m) => m.power_kw,
      render: (m) => num(m.power_kw, 1, 'kW', 'no input power reported') },
    ALARMS, VERDICT,
  ];
}

function pumps(unit: Unit): Column<PlantMachine>[] {
  const u = unit === 'c' ? '°C' : '°F';
  return [
    NAME, STATE,
    { key: 'speed', label: 'Speed', align: 'num', width: 84,
      help: 'Drive speed. Flow follows it; power follows its cube.',
      sort: (m) => m.pump_pct ?? null,
      render: (m) => num(m.pump_pct, 0, '%', 'no speed feedback') },
    { key: 'flow', label: 'Flow', align: 'num', width: 90,
      help: 'Water it is moving.',
      sort: (m) => m.flow_l_s ?? null,
      render: (m) => num(m.flow_l_s, 2, 'L/s', 'no flow meter on this pump') },
    { key: 'dp', label: 'Differential', align: 'num', width: 108,
      help: 'Discharge minus suction — the head it is developing.',
      sort: (m) => m.diff_pressure ?? null,
      render: (m) => num(m.diff_pressure, 0, 'kPa', 'no differential transmitter') },
    { key: 'motor', label: `Motor ${u}`, align: 'num', width: 92,
      help: 'Motor winding temperature.',
      sort: (m) => m.motor_temp_c ?? null,
      render: (m) => num(conv(m.motor_temp_c, unit), 1, undefined, 'no motor thermistor') },
    { key: 'power', label: 'Power', align: 'num', width: 84,
      help: 'Electrical input.',
      sort: (m) => m.power_kw,
      render: (m) => num(m.power_kw, 2, 'kW', 'no input power reported') },
    ALARMS, VERDICT,
  ];
}

function valves(unit: Unit): Column<PlantMachine>[] {
  const u = unit === 'c' ? '°C' : '°F';
  return [
    NAME, STATE,
    { key: 'cmd', label: 'Commanded', align: 'num', width: 106,
      help: 'Where the controller told it to be.',
      sort: (m) => m.commanded_pct ?? null,
      render: (m) => num(m.commanded_pct, 0, '%', 'no command feedback') },
    { key: 'pos', label: 'Position', align: 'num', width: 96,
      help: 'Where it actually is.',
      sort: (m) => m.valve_pct ?? null,
      render: (m) => num(m.valve_pct, 0, '%', 'no position feedback') },
    { key: 'dev', label: 'Deviation', align: 'num', width: 96,
      help: 'The gap between the two — the only sign the stem is not obeying.',
      sort: (m) => m.deviation_pct ?? null,
      render: (m) => (m.deviation_pct === null || m.deviation_pct === undefined
        ? <Dash why="needs both commanded and measured position" />
        : <span className={m.deviation_pct > 10 ? 'warn' : undefined}>
            {m.deviation_pct.toFixed(1)} %</span>) },
    { key: 'motor', label: `Actuator ${u}`, align: 'num', width: 100,
      help: 'Actuator temperature.',
      sort: (m) => m.motor_temp_c ?? null,
      render: (m) => num(conv(m.motor_temp_c, unit), 1, undefined, 'no actuator sensor') },
    ALARMS, VERDICT,
  ];
}

function instruments(unit: Unit): Column<PlantMachine>[] {
  const u = unit === 'c' ? '°C' : '°F';
  return [
    { ...NAME, label: 'Instrument' },
    { key: 'measures', label: 'Measures', width: 150,
      help: 'What it is tapped into. A thermowell has no opinion about the air.',
      sort: (m) => m.header ?? 'room',
      render: (m) => (
        <span className="muted">
          {m.header ? `${m.header} loop` : m.ambient_c !== null && m.ambient_c !== undefined
            ? 'Room air' : 'Instrument'}
        </span>
      ) },
    { key: 'reading', label: `Reading ${u}`, align: 'num', width: 110,
      help: 'What it is reporting now.',
      sort: (m) => m.ambient_c ?? m.supply_c ?? m.return_c ?? null,
      render: (m) => {
        const v = m.ambient_c ?? m.supply_c ?? m.return_c;
        return num(conv(v, unit), 1, undefined, 'this instrument reported nothing');
      } },
    { key: 'flow', label: 'Flow', align: 'num', width: 90,
      help: 'For a flow meter. Empty on a thermowell, which measures no flow.',
      sort: (m) => m.flow_l_s ?? null,
      render: (m) => num(m.flow_l_s, 2, 'L/s', 'this instrument measures temperature') },
    ALARMS, VERDICT,
  ];
}

/** One column that says the condition of a machine whose condition is a
 *  different quantity per kind: a battery's health, a genset's fuel and
 *  coolant, a meter's distortion. */
function Condition({ m, unit }: { m: PlantMachine; unit: Unit }) {
  const deg = unit === 'c' ? '°C' : '°F';
  const bits: React.ReactNode[] = [];
  if (m.battery_health_pct !== null && m.battery_health_pct !== undefined) {
    bits.push(<span key="bh">battery {m.battery_health_pct.toFixed(0)} %</span>);
  }
  if (m.battery_c !== null && m.battery_c !== undefined) {
    bits.push(<span key="bt" className={m.battery_c > 32 ? 'warn' : undefined}>
      {conv(m.battery_c, unit)!.toFixed(1)} {deg}</span>);
  }
  if (m.fuel_pct !== null && m.fuel_pct !== undefined) {
    bits.push(<span key="f" className={m.fuel_pct < 30 ? 'warn' : undefined}>
      fuel {m.fuel_pct.toFixed(0)} %</span>);
  }
  if (m.coolant_c !== null && m.coolant_c !== undefined) {
    bits.push(<span key="c">coolant {conv(m.coolant_c, unit)!.toFixed(0)} {deg}</span>);
  }
  if (m.thd_pct !== null && m.thd_pct !== undefined) {
    bits.push(<span key="thd" className={m.thd_pct > 5 ? 'warn' : undefined}>
      THD {m.thd_pct.toFixed(1)} %</span>);
  }
  if (m.power_factor !== null && m.power_factor !== undefined) {
    bits.push(<span key="pf">PF {m.power_factor.toFixed(2)}</span>);
  }
  if (!bits.length) return <Dash why="this machine publishes no condition point" />;
  return <span className="cond">{bits.map((b, i) => (
    <span key={i}>{i > 0 && <span className="muted"> · </span>}{b}</span>))}</span>;
}

function electrical(unit: Unit): Column<PlantMachine>[] {
  return [
    NAME,
    { key: 'kind', label: 'Kind', width: 140, sort: (m) => m.device_type,
      help: 'What the machine is.',
      render: (m) => <span className="muted">{TYPE_WORD[m.device_type] ?? m.device_type}</span> },
    STATE,
    { key: 'load', label: 'Load', align: 'num', width: 80,
      help: 'Against its own rating.',
      sort: (m) => m.duty_pct ?? null,
      render: (m) => num(m.duty_pct, 0, '%', 'no load figure published') },
    { key: 'carried', label: 'Carried', align: 'num', width: 100,
      help: 'Power measured passing THROUGH it — never its own consumption.',
      sort: (m) => m.carried_kw ?? null,
      render: (m) => num(m.carried_kw, m.carried_kw && m.carried_kw >= 100 ? 0 : 1, 'kW',
                         'this machine meters no throughput') },
    { key: 'volts', label: 'Volts', align: 'num', width: 88,
      help: 'What it is seeing on the bus.',
      sort: (m) => m.voltage_v ?? null,
      render: (m) => num(m.voltage_v, 0, 'V', 'no voltage point') },
    { key: 'cond', label: 'Condition', width: 220,
      help: 'Battery, fuel, coolant, distortion — whichever this kind has.',
      sort: (m) => m.battery_health_pct ?? m.fuel_pct ?? null,
      render: (m) => <Condition m={m} unit={unit} /> },
    ALARMS, VERDICT,
  ];
}

function carried(unit: Unit): Column<PlantMachine>[] {
  const u = unit === 'c' ? '°C' : '°F';
  return [
    { ...NAME, label: 'Device' },
    { key: 'kind', label: 'Kind', width: 150, sort: (m) => m.device_type,
      help: 'What the device is.',
      render: (m) => <span className="muted">{TYPE_WORD[m.device_type] ?? m.device_type}</span> },
    { key: 'chassis', label: `Chassis ${u}`, align: 'num', width: 104,
      help: 'Its own temperature — warmer than the room it stands in.',
      sort: (m) => m.chassis_c ?? null,
      render: (m) => num(conv(m.chassis_c, unit), 1, undefined, 'no chassis sensor') },
    { key: 'carried', label: 'Carried', align: 'num', width: 100,
      help: 'Power passing through it to the loads behind it.',
      sort: (m) => m.carried_kw ?? null,
      render: (m) => num(m.carried_kw, 1, 'kW', 'meters no throughput') },
    { key: 'monitored', label: 'Polled', align: 'mid', width: 90,
      help: 'Whether anything polls it at all. A panel with no endpoint is inventory.',
      sort: (m) => (m.monitored ? 1 : 0),
      render: (m) => (m.monitored === false
        ? <Tip className="muted" tip="no monitoring endpoint on this device — it is in inventory, not in telemetry">no</Tip>
        : <span className="muted">yes</span>) },
    ALARMS, VERDICT,
  ];
}

/** The families, in the order somebody walks a plant room: what moves heat,
 *  what moves the water, what measures it, the electrical spine, and the
 *  boxes that carry the signals. */
const FAMILIES: {
  key: string;
  title: string;
  types: string[];
  columns: (unit: Unit) => Column<PlantMachine>[];
}[] = [
  { key: 'cooling', title: 'Cooling machines',
    types: ['chiller', 'cooling_tower', 'crah', 'cdu'], columns: cooling },
  { key: 'pumps', title: 'Pumps', types: ['pump'], columns: pumps },
  { key: 'valves', title: 'Valves', types: ['valve'], columns: valves },
  { key: 'instruments', title: 'Instruments', types: ['sensor'], columns: instruments },
  { key: 'electrical', title: 'Electrical',
    types: ['utility_feed', 'switchgear', 'ats', 'ups', 'generator', 'mcc', 'mpp',
            'energy_monitor'],
    columns: electrical },
  { key: 'carried', title: 'Distribution and gateways',
    types: ['pdu', 'rpp', 'bacnet_router', 'modbus_gateway', 'oob_switch'],
    columns: carried },
];

export function RoomEquipment({ machines, unit }: {
  machines: PlantMachine[];
  unit: Unit;
}) {
  const placed = new Set<string>();
  const groups = FAMILIES.map((f) => {
    const rows = machines.filter((m) => f.types.includes(m.device_type));
    rows.forEach((m) => placed.add(m.id));
    return { ...f, rows };
  }).filter((g) => g.rows.length > 0);

  // Anything the families do not name still has to appear. A device that is
  // in the room and not on the page is the failure this whole table exists to
  // fix, and a new device type must not be able to cause it.
  const rest = machines.filter((m) => !placed.has(m.id));
  if (rest.length) {
    groups.push({ key: 'other', title: 'Other equipment',
                  types: [], columns: carried, rows: rest });
  }

  return (
    <>
      {groups.map((g) => (
        <div className="estate-panel" key={g.key}>
          <div className="estate-selected">
            <span className="who">{g.title}</span>
            <span className="muted">{g.rows.length}</span>
          </div>
          <DataTable<PlantMachine>
            rows={g.rows}
            columns={g.columns(unit)}
            lead={(m) => verdictTone(m.verdict)}
            empty="nothing of this kind in the room" />
        </div>
      ))}
    </>
  );
}
