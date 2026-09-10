/** The cooling units serving one room.
 *
 *  A CRAH stands on the floor. It is in no rack, so it appears nowhere in the
 *  rack tier - and a hall could show two open conditions while every rack
 *  under it showed none, with nothing on the page to say what was raising
 *  them. This is that table.
 *
 *  It is also the page's own stated thesis made visible. Everything above is
 *  INTAKE air: what the racks are breathing. A CRAH is judged on two other
 *  numbers, the air it discharges and the air coming back to it, and which of
 *  the two is high says different things. A high SUPPLY is the unit failing
 *  to make cold air - a valve, a coil, or chilled water that is not cold
 *  enough. A high RETURN with a good supply is the unit doing its job on air
 *  that arrives hot, which is recirculation in the room rather than a fault
 *  in the machine. Sending someone to the wrong one of those wastes the hour
 *  that matters.
 *
 *  Read from the analytics thermal view, which already classifies each unit
 *  against the room's own return p90 rather than a fixed limit: what counts
 *  as a high return depends on the hall it is in.
 *
 *  The VERDICT column is a judgement made here from the unit's own telemetry.
 *  The ALARMS column is whether anybody has been told, counted with the same
 *  predicate and the same two categories the rows above the table count with,
 *  so the units in a hall add up to the hall's figure. The two disagree in
 *  both directions and an operator needs both: a unit can read OK while
 *  carrying an open condition raised minutes ago, and one can read Supply
 *  high with nothing raised yet.
 */

import { useQuery } from '@tanstack/react-query';
import { api, type ThermalUnit } from '../../api/client';
import { Column, DataTable, Num } from '../../components/estate';
import { Tip } from '../../components/HoverTip';

type Unit = 'c' | 'f';

const conv = (c: number | null | undefined, unit: Unit) =>
  c === null || c === undefined ? null : unit === 'c' ? c : c * 9 / 5 + 32;
/** A DIFFERENCE scales by 9/5 with no offset. */
const convDelta = (c: number | null | undefined, unit: Unit) =>
  c === null || c === undefined ? null : unit === 'c' ? c : c * 9 / 5;

/** Delivered cooling: kilowatts where the model is rated, a share where it is
 *  not.
 *
 *  kW rather than % because kW is the unit the hall's load is in, so a row can
 *  be added to its neighbours and compared with the heat the room is making.
 *  The share is kept in the tip, since "68 kW" and "68 % of this machine" are
 *  both worth knowing and only one of them fits in a column.
 */
function Duty({ kw, pct, rated }: {
  kw: number | null; pct: number | null; rated: number | null;
}) {
  if (kw === null || kw === undefined) {
    if (pct === null || pct === undefined) {
      return <Tip className="dash" tip="this unit published no delivered cooling">—</Tip>;
    }
    return (
      <Tip tip={`${pct.toFixed(0)} % of this unit's rating; the platform holds no kW rating for its model`}>
        <span>{pct.toFixed(0)} %</span>
      </Tip>
    );
  }
  const share = pct ?? (rated ? (kw / rated) * 100 : null);
  const maxed = share !== null && share >= 90;
  return (
    <Tip tip={rated
      ? `${kw.toFixed(1)} kW of a ${rated.toFixed(0)} kW rating${share !== null ? `, ${share.toFixed(0)} %` : ''}`
      : `${kw.toFixed(1)} kW`}>
      <span className={maxed ? 'warn' : undefined}>{kw.toFixed(1)}</span>
    </Tip>
  );
}

/** A percentage that is diagnostic near its top end.
 *
 *  Both of these are read for HEADROOM rather than for health, so the only
 *  colour is at the ceiling: a valve that has run out of travel and a fan that
 *  has run out of speed are the two ways a unit tells you it is doing all it
 *  can. Anything below that is just where the control loop happens to be
 *  sitting, and colouring it would invent a problem.
 */
function Pct({ v, hi, why }: { v: number | null; hi: number; why: string }) {
  if (v === null || v === undefined) {
    return <Tip className="dash" tip={why}>—</Tip>;
  }
  const maxed = v >= hi;
  return (
    <Tip tip={maxed
      ? `at ${v.toFixed(0)} %, effectively wide open - this unit has no more to give`
      : `${v.toFixed(0)} % of full`}>
      <span className={maxed ? 'warn' : undefined}>{v.toFixed(0)}</span>
    </Tip>
  );
}

/** What each verdict means, in the words an operator would use. */
const VERDICT: Record<string, { label: string; tone: 'ok' | 'warn' | 'critical' }> = {
  ok: { label: 'OK', tone: 'ok' },
  high_supply: { label: 'Supply high', tone: 'critical' },
  high_return: { label: 'Return high', tone: 'warn' },
  stopped: { label: 'Stopped', tone: 'critical' },
};

export function RoomCooling({ roomId, roomName, unit }: {
  roomId: string; roomName: string; unit: Unit;
}) {
  const u = unit === 'c' ? '°C' : '°F';
  const { data, isLoading, error } = useQuery({
    queryKey: ['room-cooling', roomId],
    queryFn: () => api.thermal(roomId),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const units = data?.crah_units ?? [];
  const columns: Column<ThermalUnit & { id: string }>[] = [
    {
      key: 'name', label: 'Unit',
      help: 'One cooling unit on this room floor, and whether it is running. These stand in no rack, so they appear nowhere in the rack table above - which is how a hall could show open conditions while every rack under it showed none.',
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
      help: 'The air this unit is discharging into the cold aisle - its controlled variable, and the first point to trend. A supply above setpoint means the unit is not making cold air: check chilled water, valve and coil.',
      sort: (r) => r.supply_c,
      render: (r) => (
        <Tip tip="the air this unit is discharging into the cold aisle">
          <Num value={conv(r.supply_c, unit)} why="this unit reported no discharge air" />
        </Tip>
      ),
    },
    {
      key: 'setpoint', label: `Setpoint ${u}`, align: 'num', width: 108,
      help: 'The discharge temperature this unit is asked to hold. Configuration, not a measurement, so it does not move when the room does - which is exactly what makes any drift of Supply away from it diagnostic.',
      sort: (r) => r.setpoint_c,
      render: (r) => <Num value={conv(r.setpoint_c, unit)} why="no setpoint published" />,
    },
    {
      key: 'return', label: `Return ${u}`, align: 'num', width: 104,
      help: 'The air arriving back at the unit, which is the room\'s exhaust. A high return with a healthy supply is the hall feeding it hot air - recirculation or load - and not a fault in the machine.',
      sort: (r) => r.return_c,
      render: (r) => (
        <Tip tip="the air arriving back at the unit, which is the room's exhaust">
          <Num value={conv(r.return_c, unit)} why="this unit reported no return air" />
        </Tip>
      ),
    },
    {
      key: 'duty', label: 'Cooling kW', align: 'num', width: 108,
      help: <>Heat this unit is actually carrying away, against what it is rated to
            remove. It is not readable off the columns beside it: air-side work is
            mass flow times the air-side rise, so a unit at a wide ΔT and low
            airflow can be doing the same as one at the reverse. Read across the
            hall rather than down one row - units that should be sharing a load and
            are not is the common finding, and it is a balance problem rather than a
            capacity one. Shown as a share where the platform holds no rating for
            the model.</>,
      sort: (r) => (r.duty_kw ?? r.duty_pct),
      render: (r) => <Duty kw={r.duty_kw} pct={r.duty_pct} rated={r.rated_kw} />,
    },
    {
      key: 'valve', label: 'Valve %', align: 'num', width: 92,
      help: <>Chilled-water valve position. This is the unit's own answer to warm
            discharge air, so it is read <b>against Supply</b>: a valve pinned near
            100 % with the air still above setpoint means the water arriving is not
            cold enough or not arriving at all, and the fault is upstream in the
            plant. A valve modulating gently with the same warm air means this
            machine - a fouled coil, a failed actuator.</>,
      sort: (r) => r.valve_pct,
      render: (r) => <Pct v={r.valve_pct} hi={95}
                          why="this unit published no valve position" />,
    },
    {
      key: 'fan', label: 'Fan %', align: 'num', width: 84,
      help: <>Fan speed as a share of full. Headroom, not health: a hall holding
            its band with fans near 100 % has nothing left for the next unit that
            trips, while one sitting at the drive's turndown floor has plenty. It
            is also where a group of units shows its work - lose one and the rest
            take its share.</>,
      sort: (r) => r.fan_pct,
      render: (r) => <Pct v={r.fan_pct} hi={90}
                          why="this unit published no fan speed" />,
    },
    {
      key: 'dt', label: 'ΔT K', align: 'num', width: 88,
      help: 'Return minus supply: the heat this unit actually carried away. Low on a loaded hall means air is bypassing the racks rather than passing through them, which no amount of extra cooling fixes.',
      sort: (r) => r.delta_t_k,
      render: (r) => (
        <Tip tip="return minus supply: the heat this unit actually carried away. Low on a loaded hall is air bypassing the racks, not a cooling shortage">
          <Num value={convDelta(r.delta_t_k, unit)} />
        </Tip>
      ),
    },
    {
      key: 'alarms', label: 'Alarms', align: 'num', width: 84,
      help: 'Open cooling and environmental conditions on this unit, counted the same way the rows above count them, so the units in a hall add up to the hall\'s figure. It answers whether anybody has been told, which the verdict beside it cannot.',
      sort: (r) => r.alarms_open ?? 0,
      render: (r) => {
        const n = r.alarms_open ?? 0;
        return n
          ? <Tip tip={`${n} open cooling or environmental condition${n === 1 ? '' : 's'} on this unit`}>{n}</Tip>
          : <Tip className="dash" tip="nothing open on this unit">0</Tip>;
      },
    },
    {
      key: 'verdict', label: 'Verdict', align: 'mid', width: 130,
      help: 'Read from this unit\'s own telemetry, right now. Supply high is a unit fault, return high is a room problem, and the two send an engineer to opposite ends of the building. It can disagree with Alarms in both directions, and an operator needs both.',
      sort: (r) => r.state,
      render: (r) => {
        const v = VERDICT[r.state] ?? { label: r.state, tone: 'none' as const };
        return (
          <Tip tip={r.reason ?? 'supply and return are both where this hall expects them'}>
            <span className={v.tone === 'ok' ? undefined : v.tone}>{v.label}</span>
          </Tip>
        );
      },
    },
  ];

  const rows = units.map((x) => ({ ...x, id: x.device_id }));
  const faults = units.filter((x) => x.state !== 'ok').length;
  const roomDt = data?.room_delta_t_k ?? null;

  return (
    <div className="trend-panel">
      <h3>Cooling units in {roomName}</h3>
      <p className="muted">
        {error ? 'Could not load the cooling units.'
          : isLoading ? 'Loading the cooling units…'
            : !units.length ? 'No cooling unit reports into this room.'
              : <>
                  <b>{units.length}</b> unit{units.length === 1 ? '' : 's'}
                  {faults > 0
                    ? <>, <b>{faults}</b> not behaving</>
                    : ', all behaving'}
                  {roomDt !== null && <> · room ΔT <b>{convDelta(roomDt, unit)?.toFixed(1)}</b> {u}</>}
                  {' '}· these stand on the floor, so they appear in no rack above
                </>}
      </p>
      {!!units.length && (
        <DataTable
          rows={rows}
          columns={columns}
          lead={(r) => (VERDICT[r.state]?.tone ?? 'none') as 'ok' | 'warn' | 'critical' | 'none'}
          empty="No cooling unit reports into this room."
        />
      )}
    </div>
  );
}
