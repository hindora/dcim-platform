/** The coolant distribution units serving one room.
 *
 *  A hall with direct-to-chip servers is cooled by two chains, not one. The
 *  CRAH table above this removes heat through air; a CDU removes it through
 *  water, straight off the die, and the two fail independently. Showing only
 *  the air side described about three-quarters of the heat in these halls and
 *  silently omitted the rest.
 *
 *  It cannot be read like the air table, and that is the point of a separate
 *  one. A CRAH is judged on SUPPLY against RETURN because its return is the
 *  room handing it hot air - a finding about the floor rather than the
 *  machine. A cold-plate loop is sealed: its return is whatever the plates put
 *  in, so a wide range is a busy loop and grading it would report every rack
 *  earning its power as a cooling fault.
 *
 *  What replaces the return is the APPROACH - how close the coolant gets to
 *  the facility water it rejects into. That is the exchanger's own health, and
 *  the only column here that separates "this unit is fouling" from "the water
 *  it was given is too warm", which send an engineer to opposite ends of the
 *  building exactly as supply and return do on a CRAH. It moves first, too:
 *  a fouling exchanger holds setpoint right up until it cannot.
 *
 *  The rack tier above shows none of this. A liquid-cooled rack's heat leaves
 *  through the plate, so a CDU fault reaches the racks as throttled chips long
 *  before it reaches them as warm intake air - and on a good day, never.
 */

import { useQuery } from '@tanstack/react-query';
import { api, type LiquidUnit } from '../../api/client';
import { Column, DataTable, Num } from '../../components/estate';
import { Tip } from '../../components/HoverTip';

type Unit = 'c' | 'f';

const conv = (c: number | null | undefined, unit: Unit) =>
  c === null || c === undefined ? null : unit === 'c' ? c : c * 9 / 5 + 32;
/** A DIFFERENCE scales by 9/5 with no offset. */
const convDelta = (c: number | null | undefined, unit: Unit) =>
  c === null || c === undefined ? null : unit === 'c' ? c : c * 9 / 5;

/** Heat the plates are handing this unit, against what it is rated for.
 *
 *  kW rather than a share, because kW is the unit the hall's load is in and a
 *  CDU row has to be addable to a CRAH row. The share rides in the tip: a unit
 *  at 4 % of its rating is a fact worth knowing and does not belong in the
 *  column an operator is scanning for kilowatts.
 */
function Heat({ kw, pct, rated }: {
  kw: number | null; pct: number | null; rated: number | null;
}) {
  if (kw === null || kw === undefined) {
    return <Tip className="dash" tip="this unit published no heat load">—</Tip>;
  }
  const tip = rated
    ? `${kw.toFixed(1)} kW of a ${rated.toFixed(0)} kW unit${pct !== null ? ` — ${pct.toFixed(1)} %` : ''}`
    : `${kw.toFixed(1)} kW; the platform holds no rating for this model`;
  return <Tip tip={tip}><span>{kw.toFixed(1)}</span></Tip>;
}

function Pct({ v, hi, why, what }: {
  v: number | null; hi: number; why: string; what: string;
}) {
  if (v === null || v === undefined) {
    return <Tip className="dash" tip={why}>—</Tip>;
  }
  const maxed = v >= hi;
  return (
    <Tip tip={maxed
      ? `at ${v.toFixed(0)} %, effectively wide open — ${what}`
      : `${v.toFixed(0)} % of full`}>
      <span className={maxed ? 'warn' : undefined}>{v.toFixed(0)}</span>
    </Tip>
  );
}

/** What each verdict means, in the words an operator would use. */
const VERDICT: Record<string, { label: string; tone: 'ok' | 'warn' | 'critical' }> = {
  ok: { label: 'OK', tone: 'ok' },
  high_supply: { label: 'Coolant warm', tone: 'critical' },
  high_approach: { label: 'Approach wide', tone: 'warn' },
  restricted: { label: 'Loop restricted', tone: 'warn' },
  stopped: { label: 'Stopped', tone: 'critical' },
  unknown: { label: 'Silent', tone: 'warn' },
};

export function RoomLiquid({ roomId, roomName, unit }: {
  roomId: string; roomName: string; unit: Unit;
}) {
  const u = unit === 'c' ? '°C' : '°F';
  const { data, isLoading, error } = useQuery({
    queryKey: ['room-liquid', roomId],
    queryFn: () => api.thermal(roomId),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const units = data?.cdu_units ?? [];
  // A room with no liquid cooling is most rooms. It gets nothing at all rather
  // than an empty panel explaining an absence nobody asked about.
  if (!isLoading && !error && !units.length) return null;

  const columns: Column<LiquidUnit & { id: string }>[] = [
    {
      key: 'name', label: 'Unit',
      help: 'The coolant distribution unit, and whether its pump is turning.',
      sort: (r) => r.name,
      render: (r) => (
        <div className="name-cell">
          <span className="n">{r.name}</span>
          <span className="where">{r.running ? 'running' : 'stopped'}</span>
        </div>
      ),
    },
    {
      key: 'supply', label: `Supply ${u}`, align: 'num', width: 104,
      help: 'Coolant this unit is feeding the cold plates.',
      sort: (r) => r.supply_c,
      render: (r) => (
        <Tip tip="the coolant leaving this unit for the cold plates — warm-water cooling runs this well above the chilled water it is made from, which is what lets the chillers work at a higher COP">
          <Num value={conv(r.supply_c, unit)} why="this unit reported no loop temperatures" />
        </Tip>
      ),
    },
    {
      key: 'setpoint', label: `Setpoint ${u}`, align: 'num', width: 108,
      help: 'The coolant temperature it is trying to hold.',
      sort: (r) => r.setpoint_c,
      render: (r) => <Num value={conv(r.setpoint_c, unit)} why="no setpoint published" />,
    },
    {
      key: 'return', label: `Return ${u}`, align: 'num', width: 104,
      help: 'Coolant coming back from the plates.',
      sort: (r) => r.return_c,
      render: (r) => (
        <Tip tip="the coolant arriving back from the cold plates. A sealed loop, so this is the chips' own heat and not the room's — warm here means busy servers, not a fault">
          <Num value={conv(r.return_c, unit)} why="this unit reported no loop temperatures" />
        </Tip>
      ),
    },
    {
      key: 'dt', label: 'Range K', align: 'num', width: 96,
      help: 'Return minus supply: a result of the heat and the flow.',
      sort: (r) => r.delta_t_k,
      render: (r) => (
        <Tip tip="return minus supply. Not a setting: the pump sets the flow and the plates set this. It narrows on a lightly loaded loop, because the secondary pump floors at its turndown rather than letting the plates run dry">
          <Num value={convDelta(r.delta_t_k, unit)} />
        </Tip>
      ),
    },
    {
      key: 'flow', label: 'Flow L/s', align: 'num', width: 96,
      help: 'Secondary flow. With the range, this is the check on the heat.',
      sort: (r) => r.flow_l_s,
      render: (r) => (
        <Tip tip="coolant moving through the plates. Flow × range × 4.19 is the heat in the next column, so the three of them check each other">
          <Num value={r.flow_l_s} digits={3} why="no secondary flow published" />
        </Tip>
      ),
    },
    {
      key: 'heat', label: 'Heat kW', align: 'num', width: 100,
      help: 'Heat the plates are handing this unit.',
      sort: (r) => r.heat_kw,
      render: (r) => <Heat kw={r.heat_kw} pct={r.duty_pct} rated={r.rated_kw} />,
    },
    {
      key: 'approach', label: 'Approach K', align: 'num', width: 110,
      help: 'How close the coolant gets to the facility water. The exchanger\'s own health.',
      sort: (r) => r.approach_k,
      render: (r) => (
        <Tip tip="coolant supply above the facility water it rejects into. A few kelvin is design; widening is the exchanger fouling, air-bound or short of facility flow — and it moves while every other reading here still looks well">
          <Num value={convDelta(r.approach_k, unit)} />
        </Tip>
      ),
    },
    {
      key: 'valve', label: 'Valve %', align: 'num', width: 92,
      help: 'How far its facility-water valve is open.',
      sort: (r) => r.valve_pct,
      render: (r) => <Pct v={r.valve_pct} hi={95}
                          what="the chilled water it is being given is the constraint, not this unit"
                          why="this unit published no valve position" />,
    },
    {
      key: 'pump', label: 'Pump %', align: 'num', width: 92,
      help: 'Secondary pump speed: the loop\'s headroom.',
      sort: (r) => r.pump_pct,
      render: (r) => <Pct v={r.pump_pct} hi={90}
                          what="there is no more flow to give the plates"
                          why="this unit published no pump speed" />,
    },
    {
      key: 'alarms', label: 'Alarms', align: 'num', width: 84,
      help: 'Open cooling alarms on this unit.',
      sort: (r) => r.alarms_open ?? 0,
      render: (r) => {
        const n = r.alarms_open ?? 0;
        return n
          ? <Tip tip={`${n} open cooling or environmental condition${n === 1 ? '' : 's'} on this unit`}>{n}</Tip>
          : <Tip className="dash" tip="nothing open on this unit">0</Tip>;
      },
    },
    {
      key: 'verdict', label: 'Verdict', align: 'mid', width: 140,
      help: 'What this unit\'s own readings say right now.',
      sort: (r) => r.state,
      render: (r) => {
        const v = VERDICT[r.state] ?? { label: r.state, tone: 'none' as const };
        return (
          <Tip tip={r.reason ?? 'holding its coolant setpoint, with the approach the exchanger was designed for'}>
            <span className={v.tone === 'ok' ? undefined : v.tone}>{v.label}</span>
          </Tip>
        );
      },
    },
  ];

  const rows = units.map((x) => ({ ...x, id: x.device_id }));
  const faults = units.filter((x) => x.state !== 'ok').length;
  const heat = data?.cdu_heat_kw ?? null;

  return (
    <div className="trend-panel">
      <h3>Liquid cooling in {roomName}</h3>
      <p className="muted">
        {error ? 'Could not load the coolant units.'
          : isLoading ? 'Loading the coolant units…'
            : <>
                <b>{units.length}</b> CDU{units.length === 1 ? '' : 's'}
                {faults > 0
                  ? <>, <b>{faults}</b> not behaving</>
                  : ', all behaving'}
                {heat !== null && <> · carrying <b>{heat.toFixed(1)}</b> kW off the chips</>}
                {' '}· this heat never reaches the air, so none of it appears in the
                rack intake temperatures above
              </>}
      </p>
      {!!units.length && (
        <DataTable
          rows={rows}
          columns={columns}
          lead={(r) => (VERDICT[r.state]?.tone ?? 'none') as 'ok' | 'warn' | 'critical' | 'none'}
          empty="No coolant unit reports into this room."
        />
      )}
    </div>
  );
}
