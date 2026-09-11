/** The rooms that hold no racks, and what stands in them.
 *
 *  A generator hall has no rack intake sensor and never will, so on a table
 *  of rack intake readings it could only ever be a row of dashes that drilled
 *  into racks it does not have. That is what this replaces. Same rooms, but
 *  measured on what they actually are: a plant hall is judged on its machines,
 *  a switchroom on its boards, the roof on its towers.
 *
 *  Three columns that look similar and are not, kept apart on purpose:
 *
 *    HEAT     what the cooling machines in the room are moving
 *    POWER    what those machines are CONSUMING to do it
 *    CARRIED  what the room's incoming feed, board or UPS measures passing
 *             THROUGH it
 *
 *  Only the first two are ever added together anywhere on this page. A UPS
 *  room meters the same kilowatt at the feed, at the switchboard and at the
 *  UPS output; summing those would report three times the power the room
 *  passes, and none of it would be heat.
 *
 *  Clicking a room opens its equipment on the same table the PLANT tab's
 *  stage drill uses, with a kind column added, because a facility room mixes
 *  chillers with switchgear and the reader needs to see which is which.
 */

import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { type FacilityRoom, type PlantMachine } from '../../api/client';
import { Column, DataTable, Num, TableFoot } from '../../components/estate';
import { Tip } from '../../components/HoverTip';
import { downloadCsv, stampedName } from '../../lib/csv';
import { Kw, TYPE_WORD, Verdict, machineColumns_, usePlant, verdictTone } from './Plant';

type Unit = 'c' | 'f';

const conv = (c: number | null | undefined, unit: Unit) =>
  c === null || c === undefined ? null : unit === 'c' ? c : c * 9 / 5 + 32;

/** What the room holds, as a sentence rather than a slug list. */
function Holds({ r }: { r: FacilityRoom }) {
  const parts = Object.entries(r.by_type)
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `${n} × ${TYPE_WORD[k] ?? k}`);
  return (
    <Tip tip={<>
      {parts.map((p) => <span className="spread-line" key={p}>{p}</span>)}
      {r.unmonitored > 0 && (
        <span className="spread-line">
          <b>{r.unmonitored}</b> with no monitoring endpoint - in inventory,
          not in telemetry
        </span>
      )}
    </>}>
      <span>
        <b>{r.equipment}</b>
        {r.unmonitored > 0 && <span className="muted"> ({r.unmonitored} unmet)</span>}
      </span>
    </Tip>
  );
}

/** Cooling machines running out of installed, or a plain "none" where the
 *  room holds no cooling at all - which is most switchrooms. */
function Cooling({ r }: { r: FacilityRoom }) {
  if (!r.cooling_machines) {
    return <Tip className="dash" tip="no cooling machine stands in this room">—</Tip>;
  }
  return (
    <Tip tip={`${r.cooling_running} of ${r.cooling_machines} cooling machines in `
      + 'this room are running; the rest are staged off or stopped'}>
      <span>
        <b>{r.cooling_running}</b>
        <span className="muted">/{r.cooling_machines}</span>
      </span>
    </Tip>
  );
}

export function Facility({ unit, siteId, siteCode }: {
  unit: Unit;
  /** Limit to one site when the reader has drilled into one. */
  siteId?: string | null;
  siteCode?: string | null;
}) {
  const { data, isLoading, error } = usePlant(true);
  const [params, setParams] = useSearchParams();
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(10);
  const roomId = params.get('froom');

  const rooms = useMemo(() => {
    const all = data?.facility_rooms ?? [];
    return siteId ? all.filter((r) => r.site_id === siteId) : all;
  }, [data, siteId]);

  const room = useMemo(
    () => rooms.find((r) => r.id === roomId) ?? null, [rooms, roomId]);

  const equipment = useMemo(
    () => (room ? (data?.equipment ?? []).filter((m) => m.room_id === room.id) : []),
    [data, room]);

  function open(r: FacilityRoom) {
    setParams((prev) => {
      const q = new URLSearchParams(prev);
      q.set('froom', r.id);
      return q;
    });
    setPage(0);
  }

  function back() {
    setParams((prev) => {
      const q = new URLSearchParams(prev);
      q.delete('froom');
      return q;
    });
    setPage(0);
  }

  const u = unit === 'c' ? '°C' : '°F';

  const roomColumns: Column<FacilityRoom>[] = [
    ...(siteId ? [] : [{
      key: 'site', label: 'Site', width: 64,
      help: 'Which datacentre the room is in.',
      sort: (r: FacilityRoom) => r.site_code,
      render: (r: FacilityRoom) => <span className="muted">{r.site_code}</span>,
    }]),
    {
      key: 'name', label: 'Room', width: 170,
      help: 'A room with no racks in it.',
      sort: (r) => r.name,
      render: (r) => <b>{r.name}</b>,
    },
    {
      key: 'purpose', label: 'Purpose', width: 130,
      help: 'What the room is for, from its type in inventory.',
      sort: (r) => r.purpose,
      render: (r) => <span className="muted">{r.purpose}</span>,
    },
    {
      key: 'holds', label: 'Equipment', align: 'num', width: 110,
      help: 'How many machines stand in it. Hover for what they are.',
      sort: (r) => r.equipment,
      render: (r) => <Holds r={r} />,
    },
    {
      key: 'cooling', label: 'Cooling', align: 'num', width: 92,
      help: 'Cooling machines running, of those installed here.',
      sort: (r) => r.cooling_running,
      render: (r) => <Cooling r={r} />,
    },
    {
      key: 'heat', label: 'Heat', align: 'num', width: 92,
      help: 'Heat the cooling machines in this room are moving.',
      sort: (r) => r.heat_kw,
      render: (r) => <Kw value={r.heat_kw} why="nothing in this room moves measured heat" />,
    },
    {
      key: 'power', label: 'Power', align: 'num', width: 92,
      help: 'What those machines consume to do it.',
      sort: (r) => r.power_kw,
      render: (r) => <Kw value={r.power_kw}
                         why="nothing in this room meters its own consumption" />,
    },
    {
      key: 'carried', label: 'Carried', align: 'num', width: 100,
      help: 'Power measured passing through the room. Never added to the two before it.',
      sort: (r) => r.carried_kw,
      render: (r) => (r.carried_kw === null
        ? <Tip className="dash" tip="no feed, board or UPS in this room meters throughput">—</Tip>
        : (
          <Tip tip={`read off the room's ${TYPE_WORD[r.carried_by ?? ''] ?? r.carried_by}. `
            + 'Power passing through, not power being consumed - the same kilowatt '
            + 'is metered again at every stage downstream, so it is never added to '
            + 'the heat or the draw beside it.'}>
            <span><Num value={r.carried_kw} digits={r.carried_kw >= 100 ? 0 : 1} unit="kW" /></span>
          </Tip>
        )),
    },
    {
      key: 'chassis', label: `Chassis ${u}`, align: 'num', width: 104,
      help: 'The warmest chassis in the room. Not room air - nothing here measures that.',
      sort: (r) => r.chassis_c,
      render: (r) => (r.chassis_c === null
        ? <Tip className="dash" tip="no device in this room reports a temperature">—</Tip>
        : (
          <Tip tip={"a switch's own chassis sensor, which is the only temperature "
            + 'most plant rooms have. It runs warmer than the room around it.'}>
            <span><Num value={conv(r.chassis_c, unit)} digits={1} /></span>
          </Tip>
        )),
    },
    {
      key: 'alarms', label: 'Alarms', align: 'num', width: 80,
      help: 'Open conditions on anything in this room.',
      sort: (r) => r.alarms_open,
      render: (r) => (r.alarms_open
        ? <span className="warn">{r.alarms_open}</span>
        : <span className="muted">0</span>),
    },
    {
      key: 'verdict', label: 'Verdict', align: 'mid', width: 130,
      help: 'The worst thing standing in the room, in one word.',
      sort: (r) => r.verdict,
      render: (r) => <Verdict v={r.verdict} why={r.why} />,
    },
  ];

  const rows: (FacilityRoom | PlantMachine)[] = room ? equipment : rooms;
  const total = rows.length;
  const current = Math.min(page, Math.max(0, Math.ceil(total / pageSize) - 1));
  const visible = rows.slice(current * pageSize, (current + 1) * pageSize);

  function csv() {
    if (room) {
      downloadCsv(
        stampedName(`facility-${room.site_code}-${room.name}`),
        ['machine', 'kind', 'state', 'duty_pct', 'duty_of', 'heat_kw',
         'carried_kw', `supply_${unit}`, `return_${unit}`, 'power_kw',
         'alarms_open', 'verdict', 'why'],
        equipment.map((m) => [
          m.name, m.device_type, m.state_label ?? '', m.duty_pct ?? '',
          m.duty_of, m.heat_kw ?? '', m.carried_kw ?? '',
          conv(m.supply_c, unit)?.toFixed(1) ?? '',
          conv(m.return_c, unit)?.toFixed(1) ?? '',
          m.power_kw ?? '', m.alarms_open, m.verdict, m.why ?? '',
        ]));
      return;
    }
    downloadCsv(
      stampedName(`facility-rooms${siteCode ? `-${siteCode}` : ''}`),
      ['site', 'room', 'purpose', 'equipment', 'unmonitored', 'cooling_running',
       'cooling_machines', 'heat_kw', 'power_kw', 'carried_kw', 'carried_by',
       `chassis_${unit}`, 'alarms_open', 'verdict', 'why'],
      rooms.map((r) => [
        r.site_code, r.name, r.purpose, r.equipment, r.unmonitored,
        r.cooling_running, r.cooling_machines, r.heat_kw ?? '', r.power_kw ?? '',
        r.carried_kw ?? '', r.carried_by ?? '',
        conv(r.chassis_c, unit)?.toFixed(1) ?? '', r.alarms_open, r.verdict,
        r.why ?? '',
      ]));
  }

  if (isLoading || error || rooms.length === 0) return null;

  return (
    <div className="estate-panel">
      <div className="estate-selected">
        {room
          ? <button className="back" onClick={back}>← Facility rooms</button>
          : <span className="who">Facility rooms</span>}
        {room && (
          <span className="who">{room.name}
            <span className="where"> {room.site_code}
              {room.floor ? ` · floor ${room.floor}` : ''} · {room.purpose}</span>
          </span>
        )}
        <div className="pairs">
          {room ? (
            <>
              <span className="pair"><span className="cap">Equipment</span>
                <span className="v">{room.equipment}</span></span>
              <span className="pair"><span className="cap">Cooling</span>
                <span className="v">{room.cooling_running}/{room.cooling_machines}</span></span>
              <span className="pair"><span className="cap">Alarms</span>
                <span className="v">{room.alarms_open}</span></span>
            </>
          ) : (
            <span className="pair"><span className="cap">Rooms with no racks</span>
              <span className="v">{rooms.length}</span></span>
          )}
        </div>
      </div>

      {room
        ? (
          <DataTable<PlantMachine>
            rows={visible as PlantMachine[]}
            columns={machineColumns_(unit, true)}
            lead={(m) => verdictTone(m.verdict)}
            empty="nothing is imported into this room" />
        )
        : (
          <DataTable<FacilityRoom>
            rows={visible as FacilityRoom[]}
            columns={roomColumns}
            lead={(r) => verdictTone(r.verdict)}
            onRowClick={open}
            empty="this site has no room without racks" />
        )}

      <TableFoot total={total} page={current} pageSize={pageSize}
                 onPage={setPage} onPageSize={setPageSize} onCsv={csv}
                 noun={room ? 'machines' : 'rooms'} />
    </div>
  );
}
