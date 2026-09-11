/** The cooling plant, read as a chain rather than as a list of machines.
 *
 *  The rest of this page answers "is the air in the halls in band". This
 *  answers the question underneath it: is the machinery holding it there
 *  intact, and how much of it is spare. It exists because the ROOMS table
 *  could only ever show a plant room as a row of dashes - a chiller hall has
 *  no rack intake sensors and never will, and the machines in it were
 *  therefore invisible on the page whose entire subject they are.
 *
 *  One row per STAGE per SITE, in the direction the heat travels: hall air
 *  into the CRAHs, rack liquid into the CDUs, both into chilled water, the
 *  chillers into the condenser loop, the towers into the sky. Never one row
 *  per stage across sites - DC1's chilled-water header is not DC2's, and a
 *  mean of the two describes no header that exists.
 *
 *  Two things are deliberately NOT merged. Redundancy is judged on the
 *  RUNNING set, because a standby machine that has to start, pull down and
 *  stage on does not help in the minutes after a trip; what is available to
 *  start is counted separately and said out loud. And the air-side load and
 *  the water-side load are shown as two figures, because they are two
 *  independent measurements of the same heat and the gap between them is the
 *  instrument check - averaging them would produce a number matching neither.
 */

import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { api, type PlantMachine, type PlantPage, type PlantStage } from '../../api/client';
import { Column, DataTable, Notes, Num, TableFoot } from '../../components/estate';
import { Tip } from '../../components/HoverTip';
import { downloadCsv, stampedName } from '../../lib/csv';

type Unit = 'c' | 'f';

const conv = (c: number | null | undefined, unit: Unit) =>
  c === null || c === undefined ? null : unit === 'c' ? c : c * 9 / 5 + 32;
/** A DIFFERENCE scales by 9/5 with no offset. */
const convDelta = (c: number | null | undefined, unit: Unit) =>
  c === null || c === undefined ? null : unit === 'c' ? c : c * 9 / 5;

/** What each verdict is called on screen, in the words an engineer would use.
 *
 *  The keys are the service's, which are also the thermal page's where the two
 *  overlap: a stopped CRAH is "stopped" in both places, because a reader who
 *  sees one word here and another there has to work out whether they mean the
 *  same machine state, and they do.
 */
const VERDICT_LABEL: Record<string, string> = {
  ok: 'OK',
  n_plus_1: 'N+1',
  standby: 'Standby',
  stopped: 'Stopped',
  tripped: 'Tripped',
  alarm: 'Alarm',
  fault_signal: 'Fault signal',
  silent: 'Silent',
  tight: 'No N+1',
  no_capacity: 'None running',
  low_delta_t: 'Low ΔT',
  high_supply: 'Supply high',
  high_return: 'Return high',
  high_approach: 'Approach wide',
  low_basin: 'Basin low',
  actuator: 'Actuator',
  unknown: 'Unknown',
};

/** Tone is state, not severity theatre. Standby is deliberately neutral: a
 *  plant running fewer machines than it owns is a plant working as designed,
 *  and colouring it would teach the reader to ignore the colour. */
const VERDICT_TONE: Record<string, 'ok' | 'warn' | 'critical' | 'none'> = {
  ok: 'ok',
  n_plus_1: 'ok',
  standby: 'none',
  unknown: 'none',
  stopped: 'warn',
  silent: 'warn',
  tight: 'warn',
  alarm: 'warn',
  fault_signal: 'warn',
  low_delta_t: 'warn',
  high_return: 'warn',
  high_approach: 'warn',
  low_basin: 'warn',
  actuator: 'warn',
  tripped: 'critical',
  no_capacity: 'critical',
  high_supply: 'critical',
};

function verdictTone(v: string) {
  return VERDICT_TONE[v] ?? 'none';
}

export function Verdict({ v, why }: { v: string; why: string | null }) {
  const label = VERDICT_LABEL[v] ?? v.replace(/_/g, ' ');
  const tone = verdictTone(v);
  return (
    <Tip tip={why ?? 'nothing to report on this one'}>
      <span className={tone === 'none' ? 'muted' : tone}>{label}</span>
    </Tip>
  );
}

/** kW where the machine is rated for it, a dash where it is not.
 *
 *  A pump moves water and removes no heat; a tower rejects heat nobody
 *  measures, because there is no flow meter on it. Printing zero in either
 *  case would read as "doing nothing", which is the opposite of true.
 */
export function Kw({ value, why }: { value: number | null | undefined; why: string }) {
  if (value === null || value === undefined) {
    return <Tip className="dash" tip={why}>—</Tip>;
  }
  return <Num value={value} digits={value >= 100 ? 0 : 1} unit="kW" />;
}

/** The ΔT a stage is running at: one number where the machines share a
 *  header, the spread where they each own a loop. */
function StageDelta({ s, unit }: { s: PlantStage; unit: Unit }) {
  const u = unit === 'c' ? 'K' : '°F';
  if (s.delta_t_k !== null) {
    return (
      <Tip tip={`pooled across ${s.running} running machine${s.running === 1 ? '' : 's'} `
        + 'that share one header'}>
        <span>{convDelta(s.delta_t_k, unit)!.toFixed(1)} {u}</span>
      </Tip>
    );
  }
  if (s.delta_t_min_k === null) {
    return <Tip className="dash" tip="no running machine in this stage reports both ends of a loop">—</Tip>;
  }
  const lo = convDelta(s.delta_t_min_k, unit)!;
  const hi = convDelta(s.delta_t_max_k, unit)!;
  return (
    <Tip tip={`${s.running} independent loops, from ${lo.toFixed(1)} to ${hi.toFixed(1)} ${u}. `
      + 'Not averaged: the mean of separate loops describes none of them.'}>
      <span>{lo.toFixed(1)}–{hi.toFixed(1)}</span>
    </Tip>
  );
}

/** Running, spare, and out - with the last two kept apart.
 *
 *  "12 of 14" hides the difference between two machines deliberately staged
 *  off and two that have tripped, which is the whole question on a plant page.
 */
function Count({ s }: { s: PlantStage }) {
  const out = s.stopped - s.standby;
  return (
    <Tip tip={<>
      <span className="spread-line"><b>{s.running}</b> running of {s.machines} installed</span>
      {s.standby > 0 && (
        <span className="spread-line"><b>{s.standby}</b> healthy and staged off - available to start</span>
      )}
      {out > 0 && (
        <span className="spread-line"><b>{out}</b> stopped with something open against it</span>
      )}
      {s.rooms.length > 0 && <span className="spread-line">{s.rooms.join(', ')}</span>}
    </>}>
      <span>
        <b>{s.running}</b>
        <span className="muted">/{s.machines}</span>
        {s.standby > 0 && <span className="muted"> +{s.standby}</span>}
      </span>
    </Tip>
  );
}

/** Everything a machine publishes that has no column of its own.
 *
 *  Per type, because a tower has a wet bulb and no valve and a pump has a
 *  differential pressure and no temperatures. The column set is the same for
 *  every machine on the table; this is where the differences live.
 */
export function extras(m: PlantMachine, unit: Unit): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  const deg = unit === 'c' ? '°C' : '°F';
  const push = (k: string, body: React.ReactNode) =>
    out.push(<span className="spread-line" key={k}>{body}</span>);

  if (m.model) push('model', <span className="muted">{m.model}</span>);
  if (m.duty_pct !== null && m.duty_pct !== undefined) {
    push('duty', <>{m.duty_pct.toFixed(0)} % of {m.duty_of}</>);
  }
  if (m.cop !== null && m.cop !== undefined) push('cop', <>COP <b>{m.cop.toFixed(2)}</b></>);
  if (m.compressor_pct !== null && m.compressor_pct !== undefined) {
    push('comp', <>compressor <b>{m.compressor_pct.toFixed(0)} %</b></>);
  }
  if (m.heat_water_kw !== null && m.heat_water_kw !== undefined
      && m.heat_electrical_kw !== null && m.heat_electrical_kw !== undefined) {
    push('two', <>water side <b>{m.heat_water_kw.toFixed(0)} kW</b> · electrical side{' '}
      <b>{m.heat_electrical_kw.toFixed(0)} kW</b></>);
  }
  if (m.flow_l_s !== null && m.flow_l_s !== undefined) {
    push('flow', <>flow <b>{m.flow_l_s.toFixed(1)} L/s</b></>);
  }
  if (m.approach_k !== null && m.approach_k !== undefined) {
    push('app', <>approach <b>{convDelta(m.approach_k, unit)!.toFixed(1)}</b>
      {m.wet_bulb_c !== null && m.wet_bulb_c !== undefined
        ? <> over a {conv(m.wet_bulb_c, unit)!.toFixed(1)} {deg} wet bulb</> : null}</>);
  }
  if (m.fan_pct !== null && m.fan_pct !== undefined) {
    push('fan', <>fan <b>{m.fan_pct.toFixed(0)} %</b></>);
  }
  if (m.pump_pct !== null && m.pump_pct !== undefined) {
    push('pump', <>pump <b>{m.pump_pct.toFixed(0)} %</b></>);
  }
  if (m.valve_pct !== null && m.valve_pct !== undefined) {
    push('valve', <>valve <b>{m.valve_pct.toFixed(0)} %</b>
      {m.commanded_pct !== null && m.commanded_pct !== undefined
        ? <> against {m.commanded_pct.toFixed(0)} % commanded</> : null}</>);
  }
  if (m.diff_pressure !== null && m.diff_pressure !== undefined) {
    push('dp', <>differential <b>{m.diff_pressure.toFixed(0)} kPa</b></>);
  }
  if (m.basin_pct !== null && m.basin_pct !== undefined) {
    push('basin', <>basin <b>{m.basin_pct.toFixed(0)} %</b></>);
  }
  if (m.filter_dp !== null && m.filter_dp !== undefined) {
    push('filter', <>filter <b>{m.filter_dp.toFixed(0)} Pa</b></>);
  }
  if (m.vibration !== null && m.vibration !== undefined) {
    push('vib', <>vibration <b>{m.vibration.toFixed(2)} mm/s</b></>);
  }
  if (m.battery_c !== null && m.battery_c !== undefined) {
    push('batt', <>battery <b>{conv(m.battery_c, unit)!.toFixed(1)} {deg}</b>
      <span className="muted"> - the string, not the room</span></>);
  }
  if (m.coolant_c !== null && m.coolant_c !== undefined) {
    push('cool', <>coolant <b>{conv(m.coolant_c, unit)!.toFixed(1)} {deg}</b>
      <span className="muted"> - jacket heater while it sits</span></>);
  }
  if (m.chassis_c !== null && m.chassis_c !== undefined) {
    push('chassis', <>chassis <b>{conv(m.chassis_c, unit)!.toFixed(1)} {deg}</b>
      <span className="muted"> - its own, not the room's</span></>);
  }
  if (m.ambient_c !== null && m.ambient_c !== undefined) {
    push('ambient', <>room air <b>{conv(m.ambient_c, unit)!.toFixed(1)} {deg}</b></>);
  }
  if (m.run_hours !== null && m.run_hours !== undefined) {
    push('hrs', <span className="muted">{Math.round(m.run_hours).toLocaleString()} run hours</span>);
  }
  if (m.alarm_points.length > 0) {
    push('pts', <>asserting <b>{m.alarm_points.join(', ')}</b></>);
  }
  return out;
}

/** The machine table's columns.
 *
 *  One column set, two callers: the stage drill on the PLANT tab and a
 *  facility room's equipment list. A room mixes cooling machines with the
 *  electrical spine, so `showType` adds the kind and the STATE column reads
 *  the per-type word - "On battery", "Energised", "On generator" - rather
 *  than trying to say Running about a switchboard.
 */
export function machineColumns_(unit: Unit, showType = false,
                                showWhere = true): Column<PlantMachine>[] {
  const u = unit === 'c' ? '°C' : '°F';
  const dU = unit === 'c' ? 'K' : '°F';
  const cols: Column<PlantMachine>[] = [
  {
    key: 'name', label: 'Machine', width: 210,
    help: 'The machine, as inventory names it.',
    sort: (m) => m.name,
    render: (m) => (
      <Tip tip={<>{extras(m, unit)}</>}><b>{m.name}</b></Tip>
    ),
  },
  {
    key: 'where', label: 'Where', width: 170,
    help: 'Site and room it stands in.',
    sort: (m) => `${m.site_code} ${m.room_name ?? ''}`,
    render: (m) => <span className="muted">{m.site_code} · {m.room_name ?? '—'}</span>,
  },
  {
    key: 'state', label: 'State', align: 'mid', width: 92,
    help: 'From the machine’s own run binary, never guessed from watts.',
    sort: (m) => (m.running === null ? 2 : m.running ? 0 : 1),
    render: (m) => (m.running === null
      ? <Tip className="dash" tip="no run state inside the last-known window">—</Tip>
      : m.running
        ? <Tip tip="running">Running</Tip>
        : <Tip tip="not running"><span className="muted">Off</span></Tip>),
  },
  {
    key: 'duty', label: 'Duty', align: 'num', width: 80,
    help: 'How hard it is working - of its rating, or of full speed.',
    sort: (m) => m.duty_pct ?? null,
    render: (m) => (m.duty_pct === null || m.duty_pct === undefined
      ? <Tip className="dash" tip="this machine publishes no duty figure">—</Tip>
      : <Tip tip={`${m.duty_pct.toFixed(0)} % of ${m.duty_of}`}>
          <span>{m.duty_pct.toFixed(0)} %</span>
        </Tip>),
  },
  {
    key: 'heat', label: 'Heat', align: 'num', width: 92,
    help: 'Heat this machine is moving right now.',
    sort: (m) => m.heat_kw,
    render: (m) => <Kw value={m.heat_kw} why="this machine does not measure the heat it moves" />,
  },
  {
    key: 'supply', label: `Supply ${u}`, align: 'num', width: 96,
    help: 'What it is sending out: cold water, or cold air.',
    sort: (m) => m.supply_c,
    render: (m) => <Num value={conv(m.supply_c, unit)} digits={1}
                        why="this machine reports no supply temperature" />,
  },
  {
    key: 'return', label: `Return ${u}`, align: 'num', width: 96,
    help: 'What is coming back to it, carrying the heat it picked up.',
    sort: (m) => m.return_c,
    render: (m) => <Num value={conv(m.return_c, unit)} digits={1}
                        why="this machine reports no return temperature" />,
  },
  {
    key: 'delta', label: `ΔT ${dU}`, align: 'num', width: 86,
    help: 'Return minus supply. Low on a moving loop means flow without transfer.',
    sort: (m) => m.delta_t_k,
    render: (m) => <Num value={convDelta(m.delta_t_k, unit)} digits={1}
                        why="needs both ends of the loop" />,
  },
  {
    key: 'power', label: 'Power', align: 'num', width: 88,
    help: 'Electrical input to this machine.',
    sort: (m) => m.power_kw,
    render: (m) => <Kw value={m.power_kw} why="no input power reported" />,
  },
  {
    key: 'alarms', label: 'Alarms', align: 'num', width: 78,
    help: 'Open conditions on this machine.',
    sort: (m) => m.alarms_open,
    render: (m) => (m.alarms_open
      ? <span className="warn">{m.alarms_open}</span>
      : <span className="muted">0</span>),
  },
  {
    key: 'verdict', label: 'Verdict', align: 'mid', width: 130,
    help: 'This machine’s state, judged from its own telemetry.',
    sort: (m) => m.verdict,
    render: (m) => <Verdict v={m.verdict} why={m.why} />,
  },
  ];
  // Inside one room, every row is in that room; the header already says so.
  const kept = showWhere ? cols : cols.filter((c) => c.key !== 'where');
  if (!showType) return kept;
  return [
    kept[0],
    {
      key: 'type', label: 'Kind', width: 120,
      help: 'What the machine is. A facility room mixes several.',
      sort: (m: PlantMachine) => m.device_type,
      render: (m: PlantMachine) => (
        <span className="muted">{TYPE_WORD[m.device_type] ?? m.device_type}</span>
      ),
    },
    ...kept.slice(1),
  ];
}

/** What each device type is called on screen. Inventory's word is a slug. */
export const TYPE_WORD: Record<string, string> = {
  crah: 'CRAH', cdu: 'CDU', pump: 'Pump', valve: 'Valve',
  chiller: 'Chiller', cooling_tower: 'Cooling tower',
  ups: 'UPS', generator: 'Generator', switchgear: 'Switchgear',
  ats: 'Transfer switch', mcc: 'Motor control centre', mpp: 'Mechanical panel',
  energy_monitor: 'Meter', utility_feed: 'Incoming feed',
  sensor: 'Header instrument', bacnet_router: 'BACnet router',
  modbus_gateway: 'Modbus gateway', pdu: 'Power strip', rpp: 'Power panel',
  oob_switch: 'Access switch',
};

export function Plant({ unit }: { unit: Unit }) {
  const [params, setParams] = useSearchParams();
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(25);
  const [search, setSearch] = useState('');

  const { data, error, isLoading } = usePlant();
  const stageKey = params.get('stage');

  const stage = useMemo(
    () => (stageKey ? data?.stages.find((s) => s.id === stageKey) ?? null : null),
    [data, stageKey]);

  function open(s: PlantStage) {
    setParams((prev) => {
      const q = new URLSearchParams(prev);
      q.set('stage', s.id);
      return q;
    });
    setPage(0);
  }

  function back() {
    setParams((prev) => {
      const q = new URLSearchParams(prev);
      q.delete('stage');
      return q;
    });
    setPage(0);
  }

  const machines = useMemo(() => {
    if (!data) return [];
    const q = search.trim().toLowerCase();
    const rows = stage
      ? data.machines.filter((m) => m.site_id === stage.site_id && m.stage === stage.stage)
      : data.machines;
    if (!q) return rows;
    return rows.filter((m) => `${m.name} ${m.room_name ?? ''} ${m.site_code} `
      + `${m.device_type} ${m.model ?? ''}`.toLowerCase().includes(q));
  }, [data, stage, search]);

  const stages = useMemo(() => {
    if (!data) return [];
    const q = search.trim().toLowerCase();
    if (!q) return data.stages;
    return data.stages.filter((s) => `${s.label} ${s.site_code} ${s.rooms.join(' ')}`
      .toLowerCase().includes(q));
  }, [data, search]);

  const rows: (PlantStage | PlantMachine)[] = stage ? machines : stages;
  const total = rows.length;
  const current = Math.min(page, Math.max(0, Math.ceil(total / pageSize) - 1));
  const visible = rows.slice(current * pageSize, (current + 1) * pageSize);

  // Only the stage table's ΔT heading needs a unit word here; the machine
  // columns carry their own.
  const dU = unit === 'c' ? 'K' : '°F';

  const stageColumns: Column<PlantStage>[] = [
    {
      key: 'site', label: 'Site', width: 64,
      help: 'Which datacentre. A stage is never pooled across two.',
      sort: (s) => s.site_code,
      render: (s) => <span className="muted">{s.site_code}</span>,
    },
    {
      key: 'stage', label: 'Stage', width: 150,
      help: 'Where in the cooling chain these machines sit.',
      sort: (s) => s.label,
      render: (s) => <Tip tip={s.blurb}><b>{s.label}</b></Tip>,
    },
    {
      key: 'count', label: 'Running', align: 'num', width: 96,
      help: 'Running of installed, and how many are staged off but healthy.',
      sort: (s) => s.running,
      render: (s) => <Count s={s} />,
    },
    {
      key: 'heat', label: 'Heat', align: 'num', width: 96,
      help: 'Heat the running machines are actually moving.',
      sort: (s) => s.heat_kw,
      render: (s) => <Kw value={s.heat_kw} why="nothing in this stage measures the heat it moves" />,
    },
    {
      key: 'capacity', label: 'Capacity', align: 'num', width: 104,
      help: 'What the RUNNING machines are rated for. Standby is not capacity yet.',
      sort: (s) => s.capacity_kw,
      render: (s) => (
        <Tip tip={s.installed_kw !== null
          ? `${s.installed_kw.toFixed(0)} kW installed in this stage, of which `
            + `${(s.capacity_kw ?? 0).toFixed(0)} kW is turning`
          : 'the platform holds no rating for these machines'}>
          <span><Kw value={s.capacity_kw} why="no rating on file for the running machines" /></span>
        </Tip>
      ),
    },
    {
      key: 'duty', label: 'Duty', align: 'num', width: 80,
      help: 'Heat being moved as a share of the running machines’ rating.',
      sort: (s) => s.duty_pct,
      render: (s) => (s.duty_pct === null
        ? <Tip className="dash" tip="needs both a measured load and a rating">—</Tip>
        : <Num value={s.duty_pct} digits={0} unit="%" />),
    },
    {
      key: 'delta', label: `ΔT ${dU}`, align: 'num', width: 96,
      help: 'Across the loop. A range where each machine owns its own loop.',
      sort: (s) => s.delta_t_k ?? s.delta_t_min_k,
      render: (s) => <StageDelta s={s} unit={unit} />,
    },
    {
      key: 'power', label: 'Power', align: 'num', width: 90,
      help: 'Electrical input to these machines - the mechanical half of PUE.',
      sort: (s) => s.power_kw,
      render: (s) => <Kw value={s.power_kw} why="no machine here reported its input power" />,
    },
    {
      key: 'alarms', label: 'Alarms', align: 'num', width: 80,
      help: 'Open conditions on these machines, every category.',
      sort: (s) => s.alarms_open,
      render: (s) => (s.alarms_open
        ? <span className="warn">{s.alarms_open}</span>
        : <span className="muted">0</span>),
    },
    {
      key: 'verdict', label: 'Verdict', align: 'mid', width: 130,
      help: 'What is true about this stage, worst finding first.',
      sort: (s) => s.verdict,
      render: (s) => <Verdict v={s.verdict} why={s.why} />,
    },
  ];

  const machineColumns = machineColumns_(unit);

  function csv() {
    if (!data) return;
    if (stage) {
      downloadCsv(
        stampedName(`plant-${stage.site_code}-${stage.stage}`),
        ['machine', 'type', 'site', 'room', 'running', 'duty_pct', 'duty_of',
         'heat_kw', 'rated_kw', `supply_${unit}`, `return_${unit}`, 'delta_t_k',
         'power_kw', 'alarms_open', 'verdict', 'why'],
        machines.map((m) => [
          m.name, m.device_type, m.site_code, m.room_name ?? '',
          m.running === null ? '' : String(m.running),
          m.duty_pct ?? '', m.duty_of, m.heat_kw ?? '', m.rated_kw ?? '',
          conv(m.supply_c, unit)?.toFixed(1) ?? '',
          conv(m.return_c, unit)?.toFixed(1) ?? '',
          m.delta_t_k ?? '', m.power_kw ?? '', m.alarms_open, m.verdict,
          m.why ?? '',
        ]));
      return;
    }
    downloadCsv(
      stampedName('plant-stages'),
      ['site', 'stage', 'machines', 'running', 'standby', 'heat_kw',
       'capacity_kw', 'installed_kw', 'duty_pct', 'delta_t_k', 'delta_t_min_k',
       'delta_t_max_k', 'power_kw', 'alarms_open', 'verdict', 'why'],
      stages.map((s) => [
        s.site_code, s.label, s.machines, s.running, s.standby, s.heat_kw ?? '',
        s.capacity_kw ?? '', s.installed_kw ?? '', s.duty_pct ?? '',
        s.delta_t_k ?? '', s.delta_t_min_k ?? '', s.delta_t_max_k ?? '',
        s.power_kw ?? '', s.alarms_open, s.verdict, s.why ?? '',
      ]));
  }

  if (isLoading) return <p className="muted">Loading…</p>;
  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (!data) return null;

  return (
    <>
      <div className="estate-tools">
        {stage && (
          <button type="button" className="facility-toggle on" onClick={back}>
            ← {stage.site_code} {stage.label}
          </button>
        )}
        <input className="grow" type="search"
               placeholder={stage ? 'Search machines' : 'Search stages'}
               aria-label="Search plant" value={search}
               onChange={(e) => { setSearch(e.target.value); setPage(0); }} />
      </div>

      <div className="estate-panel">
        {stage
          ? (
            <DataTable<PlantMachine>
              rows={visible as PlantMachine[]}
              columns={machineColumns}
              lead={(m) => verdictTone(m.verdict)}
              empty="no machine in this stage matches" />
          )
          : (
            <DataTable<PlantStage>
              rows={visible as PlantStage[]}
              columns={stageColumns}
              lead={(s) => verdictTone(s.verdict)}
              onRowClick={open}
              empty="no cooling machine is imported into this estate" />
          )}
        <TableFoot total={total} page={current} pageSize={pageSize}
                   onPage={setPage} onPageSize={setPageSize} onCsv={csv}
                   noun={stage ? 'machines' : 'stages'} />
      </div>

      <Notes items={data.notes} />
    </>
  );
}

/** One query, shared by the table and the page header above it.
 *
 *  TanStack serves both callers from the same cache entry, so the KPI band and
 *  the rows underneath it always describe the same instant. A header measured
 *  at a different moment from its own table is the bug this page family exists
 *  to avoid.
 *
 *  A minute between refetches: plant machines stage on and off in minutes, not
 *  seconds, and a table that moved under the reader's hand while they read it
 *  would be worse than one a minute old.
 */
export function usePlant(enabled = true) {
  return useQuery<PlantPage>({
    queryKey: ['estate-plant'],
    queryFn: () => api.estatePlant(),
    refetchInterval: 60_000,
    // The page header asks for this before the reader has opened the tab, so
    // that the numbers are there the moment they do; on the other tabs it
    // should cost nothing at all.
    enabled,
  });
}

/** The verdict word, for a caller outside the table - the KPI band. */
export function verdictLabel(v: string | null): string | null {
  return v ? VERDICT_LABEL[v] ?? v.replace(/_/g, ' ') : null;
}

export { verdictTone };
