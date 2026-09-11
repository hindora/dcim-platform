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
import { TYPE_WORD, Verdict, machineColumns_, usePlant, verdictTone } from './Plant';

type Unit = 'c' | 'f';

const conv = (c: number | null | undefined, unit: Unit) =>
  c === null || c === undefined ? null : unit === 'c' ? c : c * 9 / 5 + 32;

/** What the room holds, as a sentence rather than a slug list.
 *
 *  The cooling count rides here rather than in a column of its own. Two of
 *  five facility rooms hold any cooling at all, so a column for it was three
 *  rows of dashes - the same emptiness this table was built to replace, only
 *  turned on its side.
 */
function Holds({ r }: { r: FacilityRoom }) {
  const parts = Object.entries(r.by_type)
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `${n} × ${TYPE_WORD[k] ?? k}`);
  return (
    <Tip tip={<>
      {parts.map((p) => <span className="spread-line" key={p}>{p}</span>)}
      {r.cooling_machines > 0 && (
        <span className="spread-line">
          <b>{r.cooling_running}</b> of {r.cooling_machines} cooling machines
          running{r.heat_kw !== null ? `, moving ${r.heat_kw.toFixed(0)} kW` : ''}
        </span>
      )}
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

/** How much of the room is turning, and how much of it is spare.
 *
 *  This replaced a Verdict column that said "Standby" on three of five rooms -
 *  which is not a finding, it is what a healthy chiller plant looks like. The
 *  split is the thing worth reading: five running and seven spare is a plant
 *  with somewhere to go, five running and none spare is not, and both of those
 *  used to print the same word.
 *
 *  The fault word did NOT go with it. A UPS on battery, a bus that has gone
 *  dead and a generator that has started are states no alarm rule in the
 *  platform covers, so they would vanish entirely if this cell only counted.
 *  They are counted separately, shown in warn, and named in the tip.
 */
function Running({ r }: { r: FacilityRoom }) {
  if (!r.machines_stated) {
    return (
      <Tip className="dash"
           tip={`nothing in this room publishes a run state - ${r.no_state} `
             + 'meter, gateway or panel, all of which are read by what they '
             + 'measure rather than by whether they are turning'}>—</Tip>
    );
  }
  return (
    <Tip tip={<>
      <span className="spread-line">
        <b>{r.active}</b> of {r.machines_stated} working
      </span>
      {r.standby > 0 && (
        <span className="spread-line">
          <b>{r.standby}</b> staged off and healthy - capacity available to start
        </span>
      )}
      {r.attention > 0 && (
        <span className="spread-line">
          <b>{r.attention}</b> neither working nor deliberately off:{' '}
          {r.attention_names.join(', ')}
        </span>
      )}
      {r.no_state > 0 && (
        <span className="spread-line">
          {r.no_state} more publish no run state - meters, gateways, panels
        </span>
      )}
    </>}>
      <span>
        <b>{r.active}</b>
        <span className="muted">/{r.machines_stated}</span>
        {r.standby > 0 && <span className="muted"> +{r.standby}</span>}
        {r.attention > 0 && <span className="warn"> · {r.attention}</span>}
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
  const [pageSize, setPageSize] = useState(25);
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
      key: 'name', label: 'Room', width: 210,
      help: 'A room with no racks in it.',
      sort: (r) => r.name,
      render: (r) => <b>{r.name}</b>,
    },
    {
      key: 'purpose', label: 'Purpose', width: 170,
      help: 'What the room is for, from its type in inventory.',
      sort: (r) => r.purpose,
      render: (r) => <span className="muted">{r.purpose}</span>,
    },
    {
      key: 'holds', label: 'Equipment', align: 'num', width: 130,
      help: 'How many machines stand in it. Hover for what they are.',
      sort: (r) => r.equipment,
      render: (r) => <Holds r={r} />,
    },
    {
      key: 'temp', label: `Temp ${u}`, align: 'num', width: 104,
      help: 'The warmest thing in the room that reports a temperature at all.',
      sort: (r) => r.temp_c,
      render: (r) => (r.temp_c === null
        ? <Tip className="dash" tip="no device in this room reports a temperature">—</Tip>
        : (
          <Tip tip={
            r.temp_source === 'room sensor'
              ? 'from an instrument that measures room air'
              : r.temp_source === 'outdoor air'
                ? 'outdoor air, off the sensor the cooling towers carry. This '
                  + 'room is outdoors, so that is not a proxy for its air - it '
                  + 'is its air.'
                : "a device's own chassis sensor, which is the only thermometer "
                  + 'most plant rooms have. It runs warmer than the room around '
                  + 'it, so it is a floor under the room temperature rather than '
                  + 'a reading of it.'}>
            <span>
              <Num value={conv(r.temp_c, unit)} digits={1} />
              {r.temp_source === 'chassis' && <span className="muted"> ch</span>}
              {r.temp_source === 'outdoor air' && <span className="muted"> oa</span>}
            </span>
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
      key: 'running', label: 'Running', align: 'num', width: 120,
      help: 'Machines working, of those that publish a state. Spare after the +.',
      sort: (r) => r.active,
      render: (r) => <Running r={r} />,
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
      ['site', 'room', 'purpose', 'equipment', 'unmonitored', 'active',
       'standby', 'attention', 'machines_stated', 'no_state', 'cooling_running',
       'cooling_machines', 'heat_kw', 'power_kw', 'carried_kw', 'carried_by',
       `temp_${unit}`, 'temp_source', 'alarms_open', 'verdict', 'why'],
      rooms.map((r) => [
        r.site_code, r.name, r.purpose, r.equipment, r.unmonitored,
        r.active, r.standby, r.attention, r.machines_stated, r.no_state,
        r.cooling_running, r.cooling_machines, r.heat_kw ?? '', r.power_kw ?? '',
        r.carried_kw ?? '', r.carried_by ?? '',
        conv(r.temp_c, unit)?.toFixed(1) ?? '', r.temp_source ?? '',
        r.alarms_open, r.verdict,
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
              <span className="pair"><span className="cap">Running</span>
                <span className="v">{room.active}/{room.machines_stated}
                  {room.standby > 0 && <span className="muted"> +{room.standby}</span>}</span></span>
              <span className="pair"><span className="cap">State</span>
                <span className="v"><Verdict v={room.verdict} why={room.why} /></span></span>
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
            columns={machineColumns_(unit, true, false)}
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
