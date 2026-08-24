import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type ThermalRow } from '../../api/client';
import {
  Column, DataTable, Delta, FacilityToggle, Notes, Num, PageHead, ScopeTabs, Seg,
  TableFoot, tone,
} from '../../components/estate';
import { downloadCsv, stampedName } from '../../lib/csv';
import { useEstateTable } from './useEstateTable';

/** Thermal: how warm the estate is running, and how much of it is in band.
 *
 *  Compliance is the share of intake READINGS inside the ASHRAE recommended
 *  envelope, not the share of racks. A rack polled every ten seconds and one
 *  polled hourly are not equally strong evidence, and weighting them equally
 *  would let a quiet sensor outvote a busy one.
 *
 *  There is no humidity row and no composite score. Nothing in this estate
 *  measures humidity, and a score would be a number we invented sitting beside
 *  four that were measured.
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

export function Thermal() {
  const [mode, setMode] = useState<'daily' | 'live'>('live');
  const [unit, setUnit] = useState<Unit>('c');
  const [focus, setFocus] = useState<string>('');
  const [compare, setCompare] = useState<string>('');

  const { data, isLoading, error } = useQuery({
    queryKey: ['estate-thermal', mode, focus, compare],
    queryFn: () => api.estateThermal({
      mode,
      focus: mode === 'daily' ? (focus || undefined) : undefined,
      compare: mode === 'daily' ? (compare || undefined) : undefined,
    }),
    refetchInterval: mode === 'live' ? 60_000 : false,
  });

  const t = useEstateTable<ThermalRow>(data?.sites ?? [], data?.rooms ?? []);
  const u = unit === 'c' ? '°C' : '°F';

  const columns: Column<ThermalRow>[] = [
    {
      key: 'name', label: t.scope === 'sites' && !t.selected ? 'Site' : 'Room',
      sort: (r) => r.name,
      render: (r) => (
        <div className="name-cell">
          <span className="n">{r.name}</span>
          {r.kind === 'room' && <span className="where">{r.site_code}{r.floor ? ` · floor ${r.floor}` : ''}</span>}
        </div>
      ),
    },
    {
      key: 'racks', label: 'Racks', align: 'mid', width: 80,
      sort: (r) => r.rack_count ?? 0,
      render: (r) => r.rack_count ?? 0,
    },
    {
      key: 'avg', label: `Average ${u}`, align: 'num', width: 130,
      sort: (r) => r.avg_c,
      render: (r) => <Num value={conv(r.avg_c, unit)} why={r.note} />,
    },
    {
      key: 'davg', label: 'Δ avg', align: 'num', width: 92,
      sort: (r) => r.delta_avg,
      render: (r) => <Delta value={convDelta(r.delta_avg, unit)} />,
    },
    {
      key: 'max', label: `Max ${u}`, align: 'num', width: 120,
      sort: (r) => r.max_c,
      render: (r) => <Num value={conv(r.max_c, unit)} why={r.note} />,
    },
    {
      key: 'dmax', label: 'Δ max', align: 'num', width: 92,
      sort: (r) => r.delta_max,
      render: (r) => <Delta value={convDelta(r.delta_max, unit)} />,
    },
    {
      key: 'compliance', label: 'In band', align: 'num', width: 110,
      sort: (r) => r.compliance_pct,
      render: (r) => <Num value={r.compliance_pct} digits={1} unit="%" why={r.note} />,
    },
    {
      key: 'samples', label: 'Readings', align: 'num', width: 110,
      sort: (r) => r.samples,
      render: (r) => (r.samples ? r.samples.toLocaleString() : <span className="dash">—</span>),
    },
  ];

  function exportCsv() {
    downloadCsv(
      stampedName('thermal', data?.window.label),
      ['scope', 'name', 'site', 'floor', 'racks', `average_${unit}`, `max_${unit}`,
       'in_band_pct', 'readings', 'delta_avg_c', 'delta_max_c', 'note'],
      t.filtered.map((r) => [
        r.kind, r.name, r.site_code, r.floor ?? '', r.rack_count ?? '',
        conv(r.avg_c, unit)?.toFixed(1) ?? '', conv(r.max_c, unit)?.toFixed(1) ?? '',
        r.compliance_pct ?? '', r.samples, r.delta_avg ?? '', r.delta_max ?? '',
        r.note ?? '',
      ]),
    );
  }

  const totals = data?.totals;
  return (
    <div className="estate">
      <PageHead
        title="Thermal"
        sub={<>Intake air across the estate. Band {data?.band.low_c ?? 18}–{data?.band.high_c ?? 27} °C
          {' '}({data?.band.basis ?? 'ASHRAE recommended'}).{' '}
          <Link to="/analytics">Rack-level ΔT and hot spots →</Link></>}
        kpis={[
          { caption: 'Average', value: conv(totals?.avg_c ?? null, unit), unit: u,
            why: 'no rack intake sensor reported in this window' },
          { caption: 'Max', value: conv(totals?.max_c ?? null, unit), unit: u,
            tone: (totals?.max_c ?? 0) > (data?.band.high_c ?? 27) ? 'warn' : undefined },
          { caption: 'In band', value: totals?.compliance_pct ?? null, unit: '%',
            tone: (totals?.compliance_pct ?? 100) < 95 ? 'warn' : 'ok' },
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
        <input className="grow" type="search" placeholder="Search sites and rooms"
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
        {t.selected && (
          <div className="estate-selected">
            <button className="back" onClick={t.clearDrill}>← All sites</button>
            <span className="who">{t.selected.name}</span>
            <div className="pairs">
              <span className="pair"><span className="cap">Average</span>
                <span className="v"><Num value={conv(t.selected.avg_c, unit)} /> {u}</span></span>
              <span className="pair"><span className="cap">Max</span>
                <span className="v"><Num value={conv(t.selected.max_c, unit)} /> {u}</span></span>
              <span className="pair"><span className="cap">In band</span>
                <span className="v"><Num value={t.selected.compliance_pct} unit="%" /></span></span>
            </div>
          </div>
        )}

        <DataTable
          rows={t.visible}
          columns={columns}
          lead={(r) => {
            if (r.avg_c === null) return 'none';
            if (r.max_c !== null && r.max_c > (data?.band.high_c ?? 27)) return 'critical';
            return tone(r.compliance_pct === null ? null : 100 - r.compliance_pct);
          }}
          onRowClick={(r) => (r.kind === 'site' ? t.drillInto(r) : undefined)}
          empty={isLoading ? 'Loading…'
            : error ? 'Could not load thermal data.'
            : 'Nothing matches this search.'}
        />

        <TableFoot total={t.filtered.length} page={t.page} pageSize={t.pageSize}
                   onPage={t.setPage} onPageSize={t.setPageSize} onCsv={exportCsv} />
      </div>

      <Notes items={[
        ...(data?.notes ?? []),
        data ? `Window: ${data.window.label}, compared with ${data.window.compare_label}. `
             + 'Days are UTC so every row covers the same 24 hours.' : '',
      ].filter(Boolean)} />
    </div>
  );
}
