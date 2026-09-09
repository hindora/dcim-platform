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
      sort: (r) => r.supply_c,
      render: (r) => (
        <Tip tip="the air this unit is discharging into the cold aisle">
          <Num value={conv(r.supply_c, unit)} why="this unit reported no discharge air" />
        </Tip>
      ),
    },
    {
      key: 'setpoint', label: `Setpoint ${u}`, align: 'num', width: 108,
      sort: (r) => r.setpoint_c,
      render: (r) => <Num value={conv(r.setpoint_c, unit)} why="no setpoint published" />,
    },
    {
      key: 'return', label: `Return ${u}`, align: 'num', width: 104,
      sort: (r) => r.return_c,
      render: (r) => (
        <Tip tip="the air arriving back at the unit, which is the room's exhaust">
          <Num value={conv(r.return_c, unit)} why="this unit reported no return air" />
        </Tip>
      ),
    },
    {
      key: 'dt', label: 'ΔT K', align: 'num', width: 88,
      sort: (r) => r.delta_t_k,
      render: (r) => (
        <Tip tip="return minus supply: the heat this unit actually carried away. Low on a loaded hall is air bypassing the racks, not a cooling shortage">
          <Num value={convDelta(r.delta_t_k, unit)} />
        </Tip>
      ),
    },
    {
      key: 'alarms', label: 'Alarms', align: 'num', width: 84,
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
