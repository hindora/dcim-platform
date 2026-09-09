import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';
import { api, type ThermalRow, type ThermalSpread } from '../../api/client';
import {
  Column, DataTable, Delta, FacilityToggle, Notes, Num, PageHead, ScopeTabs, Seg,
  TableFoot, tone,
} from '../../components/estate';
import { Tip } from '../../components/HoverTip';
import { downloadCsv, stampedName } from '../../lib/csv';
import { RoomDrawer } from '../home/RoomDrawer';
import { useEstateTable } from './useEstateTable';

/** Thermal: how warm the estate is running, and how much of it is in band.
 *
 *  Compliance is the share of intake READINGS inside the ASHRAE recommended
 *  envelope, not the share of racks. A rack polled every ten seconds and one
 *  polled hourly are not equally strong evidence, and weighting them equally
 *  would let a quiet sensor outvote a busy one.
 *
 *  Relative humidity comes from the rack PDU environment probes and is shown
 *  beside compliance, not folded into it. There is no composite score: it
 *  would be a number we invented sitting beside five that were measured.
 *
 *  Two ceilings, two tones. Above the recommended band (27 C) a row is warn;
 *  above the allowable ceiling (32 C) it is critical. Those are the same two
 *  lines the inlet temperature alarm rules draw, so the table and the alarm
 *  list never disagree about how bad a number is.
 *
 *  Intake per rack comes from the rack's front environment probe where one
 *  reported, else from the servers' BMC inlet, and each row says which. That
 *  is the order a DCIM uses: the probe is the cold aisle at the rack face,
 *  the BMC is behind the bezel and blind in a rack with no servers. Rooms
 *  and sites are the racks added up, so the tiers cannot disagree.
 *
 *  Average and Max hide the most common real finding, overcooling, so each
 *  row also carries the 90th percentile of its readings and a four-way
 *  spread against the ASHRAE lines: below the recommended floor, in band,
 *  above it but allowable, above the allowable ceiling. The below share is
 *  printed on its own because it is the evidence a site raises a setpoint
 *  on. p90 is taken over the row's pooled readings in one query per tier,
 *  never summarised from the tier below.
 *
 *  Three tiers: sites, rooms, racks. A rack is where an engineer actually
 *  goes, so the table does not stop one level short of it. Rack rows add
 *  exhaust, ΔT and the sensor count, and a rack row opens its elevation with
 *  the thermal overlay already on. The room drawer stays one click away from
 *  the drilled-in header.
 */

type Unit = 'c' | 'f';

function conv(c: number | null, unit: Unit): number | null {
  if (c === null || c === undefined) return null;
  return unit === 'c' ? c : c * 9 / 5 + 32;
}

/** A DIFFERENCE in temperature scales by 9/5 with no offset.
 *
 *  Running a delta through the absolute conversion adds 32 to a change, which
 *  turns "half a degree warmer than yesterday" into "32.9 degrees warmer".
 */
function convDelta(c: number | null, unit: Unit): number | null {
  if (c === null || c === undefined) return null;
  return unit === 'c' ? c : c * 9 / 5;
}

/** The four-way split as one partition bar, with the figures in the tip.
 *
 *  Hue is state here: cool for overcooled, ok for in band, warn and
 *  critical for the two ceilings - the same two lines the row's lead
 *  colour and the inlet alarm rules use. Zero-width segments are simply
 *  absent; the bar is a partition, not four bars.
 */
function Spread({ d, low, high, allowable }: {
  d: ThermalSpread | null | undefined; low: number; high: number; allowable: number;
}) {
  if (!d) return <Tip className="dash" tip="no intake readings in this window">—</Tip>;
  const segs: Array<[string, number, string]> = [
    ['cool', d.below_pct, `below ${low} °C (overcooled)`],
    ['ok', d.in_band_pct, `${low}–${high} °C recommended`],
    ['warn', d.above_recommended_pct, `${high}–${allowable} °C allowable`],
    ['critical', d.above_allowable_pct, `above ${allowable} °C`],
  ];
  return (
    <Tip tip={<>{segs.map(([k, v, label]) => (
      <span key={k} className="spread-line"><b>{v.toFixed(1)}%</b> {label}</span>
    ))}</>}>
      <span className="stack-bar cell" role="img"
            aria-label={`${d.below_pct}% overcooled, ${d.in_band_pct}% in band, ${d.above_recommended_pct}% above recommended, ${d.above_allowable_pct}% above allowable`}>
        {segs.filter(([, v]) => v > 0).map(([k, v]) => (
          <span key={k} className={`stack-seg ${k}`} style={{ width: `${v}%` }} />
        ))}
      </span>
    </Tip>
  );
}

export function Thermal() {
  const [mode, setMode] = useState<'daily' | 'live'>('live');
  const [unit, setUnit] = useState<Unit>('c');
  const [focus, setFocus] = useState<string>('');
  const [compare, setCompare] = useState<string>('');
  const [drawerRoom, setDrawerRoom] = useState<{ id: string; name: string } | null>(null);
  const navigate = useNavigate();

  const { data, isLoading, error } = useQuery({
    queryKey: ['estate-thermal', mode, focus, compare],
    queryFn: () => api.estateThermal({
      mode,
      focus: mode === 'daily' ? (focus || undefined) : undefined,
      compare: mode === 'daily' ? (compare || undefined) : undefined,
    }),
    refetchInterval: mode === 'live' ? 60_000 : false,
  });

  const t = useEstateTable<ThermalRow>(data?.sites ?? [], data?.rooms ?? [], data?.racks ?? []);
  const rackTier = t.tier === 'racks';
  const u = unit === 'c' ? '°C' : '°F';
  const floor = data?.band.low_c ?? 18;
  const recommended = data?.band.high_c ?? 27;
  const allowable = data?.band.allowable_high_c ?? 32;
  const rhHigh = data?.band.rh_high_pct ?? 60;

  /** warn above recommended, critical above allowable, else by in-band share. */
  function severity(r: ThermalRow): 'ok' | 'warn' | 'critical' | 'none' {
    if (r.avg_c === null) return 'none';
    if (r.max_c !== null && r.max_c > allowable) return 'critical';
    if (r.max_c !== null && r.max_c > recommended) return 'warn';
    return tone(r.compliance_pct === null ? null : 100 - r.compliance_pct);
  }

  const tierLabel = { sites: 'Site', rooms: 'Room', racks: 'Rack' }[t.tier];
  const columns: Column<ThermalRow>[] = [
    {
      key: 'name', label: tierLabel,
      sort: (r) => r.name,
      render: (r) => (
        <div className="name-cell">
          <span className="n">{r.name}</span>
          {r.kind === 'room' && <span className="where">{r.site_code}{r.floor ? ` · floor ${r.floor}` : ''}</span>}
          {r.kind === 'rack' && <span className="where">{r.room_name}{r.row ? ` · row ${r.row}` : ''}{r.u_height ? ` · ${r.u_height}U` : ''}</span>}
        </div>
      ),
    },
    ...(rackTier ? [{
      // What spoke for the rack, and how many of them. The word is the
      // source: a probe is the intake reading, servers are the fallback.
      key: 'sensors', label: 'Intake from', align: 'mid' as const, width: 108,
      sort: (r: ThermalRow) => r.sensors ?? 0,
      render: (r: ThermalRow) => r.source ? (
        <Tip tip={r.source === 'probes'
          ? 'the rack\'s front environment probe: the cold aisle at the rack face, which is what ASHRAE calls intake'
          : 'server BMC inlet sensors, used because no rack probe reported; behind the bezel and a degree or two warm'}>
          {r.sensors} {r.source === 'probes'
            ? (r.sensors === 1 ? 'probe' : 'probes')
            : (r.sensors === 1 ? 'server' : 'servers')}
        </Tip>
      ) : <Tip className="dash" tip="no probe and no server intake sensor reported">—</Tip>,
    }] : [{
      key: 'racks', label: 'Racks', align: 'mid' as const, width: 80,
      sort: (r: ThermalRow) => r.rack_count ?? 0,
      render: (r: ThermalRow) => r.sources ? (
        <Tip tip={<>{r.sources.probes} by rack probe · {r.sources.servers} by servers
                  · {(r.rack_count ?? 0) - r.sources.probes - r.sources.servers} silent</>}>
          {r.rack_count ?? 0}
        </Tip>
      ) : (r.rack_count ?? 0),
    }]),
    {
      key: 'avg', label: `Average ${u}`, align: 'num', width: 104,
      sort: (r) => r.avg_c,
      render: (r) => <Num value={conv(r.avg_c, unit)} why={r.note} />,
    },
    {
      key: 'davg', label: 'Δ avg', align: 'num', width: 84,
      sort: (r) => r.delta_avg,
      render: (r) => <Delta value={convDelta(r.delta_avg, unit)} why={r.delta_note} />,
    },
    {
      key: 'p90', label: `p90 ${u}`, align: 'num', width: 92,
      sort: (r) => r.p90_c,
      render: (r) => (
        <Tip tip={r.p90_c === null ? (r.note ?? 'no intake readings in this window')
          : '90th percentile of this row\'s pooled intake readings: what it runs at without one sensor\'s spike deciding'}>
          <span className={r.p90_c !== null && r.p90_c > allowable ? 'critical'
            : r.p90_c !== null && r.p90_c > recommended ? 'warn' : undefined}>
            <Num value={conv(r.p90_c, unit)} />
          </span>
        </Tip>
      ),
    },
    {
      key: 'max', label: `Max ${u}`, align: 'num', width: 96,
      sort: (r) => r.max_c,
      render: (r) => <Num value={conv(r.max_c, unit)} why={r.note} />,
    },
    {
      key: 'dmax', label: 'Δ max', align: 'num', width: 84,
      sort: (r) => r.delta_max,
      render: (r) => <Delta value={convDelta(r.delta_max, unit)} why={r.delta_note} />,
    },
    ...(rackTier ? [
      {
        key: 'exhaust', label: `Exhaust ${u}`, align: 'num' as const, width: 104,
        sort: (r: ThermalRow) => r.exhaust_c ?? null,
        render: (r: ThermalRow) => <Num value={conv(r.exhaust_c ?? null, unit)}
                                        why="no exhaust sensor reported in this window" />,
      },
      {
        key: 'dt', label: 'ΔT K', align: 'num' as const, width: 76,
        sort: (r: ThermalRow) => r.delta_t_k ?? null,
        render: (r: ThermalRow) => (
          <Tip tip="server exhaust minus this row's intake - low on a loaded rack is bypass air, not a cooling shortage">
            <Num value={convDelta(r.delta_t_k ?? null, unit)} />
          </Tip>
        ),
      },
    ] : []),
    {
      key: 'compliance', label: 'In band', align: 'num', width: 96,
      sort: (r) => r.compliance_pct,
      render: (r) => <Num value={r.compliance_pct} digits={1} unit="%" why={r.note} />,
    },
    {
      key: 'below', label: 'Below band', align: 'num', width: 100,
      sort: (r) => r.below_pct,
      render: (r) => (
        <Tip tip={r.below_pct === null ? (r.note ?? 'no intake readings in this window')
          : `share of readings under ${floor} °C: overcooled air, which costs fan and chiller energy and is the evidence for raising a setpoint`}>
          <Num value={r.below_pct} digits={1} unit="%" />
        </Tip>
      ),
    },
    {
      key: 'spread', label: 'Spread', align: 'mid', width: 116,
      // Sorted by how much of the row sits OUTSIDE the recommended band,
      // either side, so the rows worth a look come first.
      sort: (r) => (r.compliance_pct === null ? null : 100 - r.compliance_pct),
      render: (r) => <Spread d={r.distribution} low={floor} high={recommended} allowable={allowable} />,
    },
    {
      key: 'rh', label: 'RH %', align: 'num', width: 84,
      sort: (r) => r.rh_avg,
      render: (r) => (
        <Tip tip={r.rh_avg === null
          ? 'no rack humidity probe reported in this window'
          : <>max <b>{r.rh_max}%</b> · {r.rh_probes} probe{r.rh_probes === 1 ? '' : 's'}
               · recommended ceiling {rhHigh}%</>}>
          <span className={r.rh_max !== null && r.rh_max > rhHigh ? 'warn' : undefined}>
            <Num value={r.rh_avg} />
          </span>
        </Tip>
      ),
    },
    {
      key: 'samples', label: 'Readings', align: 'num', width: 96,
      sort: (r) => r.samples,
      render: (r) => (r.samples ? r.samples.toLocaleString() : <span className="dash">—</span>),
    },
  ];

  function exportCsv() {
    downloadCsv(
      stampedName('thermal', data?.window.label),
      ['scope', 'name', 'site', 'room', 'floor', 'racks', 'source', 'sensors', `average_${unit}`,
       `p90_${unit}`, `max_${unit}`, `exhaust_${unit}`, 'delta_t_k', 'in_band_pct',
       'below_band_pct', 'above_recommended_pct', 'above_allowable_pct', 'rh_avg_pct',
       'rh_max_pct', 'rh_probes', 'readings', 'delta_avg_c', 'delta_max_c', 'note'],
      t.filtered.map((r) => [
        r.kind, r.name, r.site_code, r.room_name ?? '', r.floor ?? '', r.rack_count ?? '',
        r.source ?? '', r.sensors ?? '',
        conv(r.avg_c, unit)?.toFixed(1) ?? '', conv(r.p90_c, unit)?.toFixed(1) ?? '',
        conv(r.max_c, unit)?.toFixed(1) ?? '',
        conv(r.exhaust_c ?? null, unit)?.toFixed(1) ?? '', r.delta_t_k ?? '',
        r.compliance_pct ?? '', r.below_pct ?? '',
        r.distribution?.above_recommended_pct ?? '', r.distribution?.above_allowable_pct ?? '',
        r.rh_avg ?? '', r.rh_max ?? '', r.rh_probes,
        r.samples, r.delta_avg ?? '', r.delta_max ?? '',
        r.note ?? '',
      ]),
    );
  }

  const totals = data?.totals;
  return (
    <div className="estate">
      <PageHead
        title="Thermal"
        sub={<>Intake air across the estate. Band {data?.band.low_c ?? 18}–{recommended} °C,
          {' '}allowable to {allowable} °C ({data?.band.basis ?? 'ASHRAE recommended'}).{' '}
          <Link to="/analytics?view=thermal">Rack-level ΔT and hot spots →</Link></>}
        // Seven KPIs on the title row. The header lets the subtitle wrap
        // before it lets the band drop under the title.
        kpis={[
          { caption: 'Average', value: conv(totals?.avg_c ?? null, unit), unit: u,
            why: 'no rack intake sensor reported in this window' },
          { caption: 'p90', value: conv(totals?.p90_c ?? null, unit), unit: u,
            tone: (totals?.p90_c ?? 0) > allowable ? 'critical'
              : (totals?.p90_c ?? 0) > recommended ? 'warn' : undefined,
            why: 'no rack intake sensor reported in this window' },
          { caption: 'Max', value: conv(totals?.max_c ?? null, unit), unit: u,
            tone: (totals?.max_c ?? 0) > allowable ? 'critical'
              : (totals?.max_c ?? 0) > recommended ? 'warn' : undefined },
          { caption: 'In band', value: totals?.compliance_pct ?? null, unit: '%',
            tone: (totals?.compliance_pct ?? 100) < 95 ? 'warn' : 'ok' },
          { caption: 'Below band', value: totals?.below_pct ?? null, unit: '%',
            why: 'no rack intake sensor reported in this window' },
          { caption: 'RH', value: totals?.rh_avg ?? null, unit: '%',
            tone: (totals?.rh_max ?? 0) > rhHigh ? 'warn' : undefined,
            why: 'no rack humidity probe reported in this window' },
          // White space only: rack intake sensors exist where racks do, so
          // counting a generator room as a room that failed to report made the
          // ratio read as a fleet of dead sensors.
          { caption: 'Halls reporting',
            value: totals ? `${totals.rooms_reporting}/${totals.rooms}` : null,
            tone: totals && totals.rooms_reporting < totals.rooms ? 'warn' : 'ok' },
        ]}
      />

      <div className="estate-tools">
        <ScopeTabs scope={t.scope} onChange={t.setScope} />
        {(t.scope === 'rooms' || t.selected) && (
          <FacilityToggle on={t.includeFacility} count={t.facilityCount}
                          onChange={t.setIncludeFacility} />
        )}
        <input className="grow" type="search"
               placeholder={rackTier ? 'Search racks' : 'Search sites and rooms'}
               aria-label="Search" value={t.search}
               onChange={(e) => t.setSearch(e.target.value)} />
        <Seg label="Unit" value={unit} onChange={setUnit}
             options={[{ key: 'c', label: '°C' }, { key: 'f', label: '°F' }]} />
        <Seg label="Window" value={mode} onChange={setMode}
             options={[{ key: 'live', label: 'LAST HOUR' }, { key: 'daily', label: 'BY DAY' }]} />
        {mode === 'daily' && (
          <>
            <label className="field">
              <span>Focus day</span>
              <input type="date" value={focus} onChange={(e) => setFocus(e.target.value)} />
            </label>
            <label className="field">
              <span>Compare with</span>
              <input type="date" value={compare} onChange={(e) => setCompare(e.target.value)} />
            </label>
          </>
        )}
      </div>

      <div className="estate-panel">
        {(t.selectedRoom ?? t.selected) && (() => {
          const head = (t.selectedRoom ?? t.selected)!;
          const back = t.selectedRoom
            ? (t.selected ? `← ${t.selected.site_code} rooms` : '← All rooms')
            : '← All sites';
          return (
            <div className="estate-selected">
              <button className="back" onClick={t.clearDrill}>{back}</button>
              <span className="who">
                {head.name}
                {t.selectedRoom && <span className="where"> {head.site_code}{head.floor ? ` · floor ${head.floor}` : ''}</span>}
              </span>
              {t.selectedRoom && (
                <button className="back" title="Devices, alarms and trend for this room"
                        onClick={() => setDrawerRoom({ id: head.id, name: head.name })}>
                  ROOM DETAILS
                </button>
              )}
              <div className="pairs">
                <span className="pair"><span className="cap">Average</span>
                  <span className="v"><Num value={conv(head.avg_c, unit)} /> {u}</span></span>
                <span className="pair"><span className="cap">p90</span>
                  <span className="v"><Num value={conv(head.p90_c, unit)} /> {u}</span></span>
                <span className="pair"><span className="cap">Max</span>
                  <span className="v"><Num value={conv(head.max_c, unit)} /> {u}</span></span>
                <span className="pair"><span className="cap">In band</span>
                  <span className="v"><Num value={head.compliance_pct} unit="%" /></span></span>
                <span className="pair"><span className="cap">Below</span>
                  <span className="v"><Num value={head.below_pct} unit="%" /></span></span>
                <span className="pair"><span className="cap">RH</span>
                  <span className="v"><Num value={head.rh_avg} unit="%" /></span></span>
              </div>
            </div>
          );
        })()}

        <DataTable
          rows={t.visible}
          columns={columns}
          lead={severity}
          // Site -> its rooms, room -> its racks, rack -> its elevation with
          // the thermal overlay on. The room drawer is the button in the
          // drilled-in header, so it is one click away rather than the click.
          onRowClick={(r) => (r.kind === 'rack'
            ? navigate(`/racks/${r.id}?overlay=thermal&from=thermal`)
            : t.drillInto(r))}
          empty={isLoading ? 'Loading…'
            : error ? 'Could not load thermal data.'
            : 'Nothing matches this search.'}
        />

        <TableFoot total={t.filtered.length} page={t.page} pageSize={t.pageSize}
                   noun={t.tier}
                   onPage={t.setPage} onPageSize={t.setPageSize} onCsv={exportCsv} />
      </div>

      <Notes items={[
        ...(data?.notes ?? []),
        data ? `Window: ${data.window.label}, compared with ${data.window.compare_label}. `
             + 'Days are UTC so every row covers the same 24 hours.' : '',
      ].filter(Boolean)} />

      {drawerRoom && (
        <RoomDrawer roomId={drawerRoom.id} roomName={drawerRoom.name}
                    onClose={() => setDrawerRoom(null)} />
      )}
    </div>
  );
}
