import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type PowerRow } from '../../api/client';
import { Tip } from '../../components/HoverTip';
import {
  Column, DataTable, Delta, FacilityToggle, Notes, Num, PageHead, ScopeTabs, Seg,
  TableFoot,
} from '../../components/estate';
import { downloadCsv, stampedName } from '../../lib/csv';
import { useEstateTable } from './useEstateTable';

/** Power: where the estate's kilowatts go, and what that does to PUE.
 *
 *  Three modes, because they answer different questions. NOW is the live draw
 *  from the hot mirror. AVERAGE is the mean over a window - what the estate
 *  costs to run. PEAK is the COINCIDENT maximum: loads summed per bucket, then
 *  the largest bucket taken, so the figure is one that actually happened
 *  rather than a sum of each device's private worst moment.
 */

type Mode = 'now' | 'average' | 'peak';

/** Local midnight, `n` days back, as the value a date input wants. */
function daysAgo(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}

export function Power() {
  const [mode, setMode] = useState<Mode>('now');
  const [from, setFrom] = useState(daysAgo(1));
  const [to, setTo] = useState(daysAgo(0));

  const { data, isLoading, error } = useQuery({
    queryKey: ['estate-power', mode, from, to],
    queryFn: () => api.estatePower(
      mode === 'now'
        ? { live: true }
        : {
            mode,
            start: new Date(`${from}T00:00:00Z`).toISOString(),
            // Inclusive of the end day: an operator picking 21st to 21st means
            // that whole day, not the empty instant at its start.
            end: new Date(`${to}T00:00:00Z`).toISOString(),
          },
    ),
    refetchInterval: mode === 'now' ? 30_000 : false,
  });

  const t = useEstateTable<PowerRow>(data?.sites ?? [], data?.rooms ?? []);

  const columns: Column<PowerRow>[] = [
    {
      key: 'name', label: t.scope === 'sites' && !t.selected ? 'Site' : 'Room',
      sort: (r) => r.name,
      render: (r) => (
        <div className="name-cell">
          <span className="n">{r.name}</span>
          {r.kind === 'room' && (
            <span className="where">{r.site_code}{r.floor ? ` · floor ${r.floor}` : ''}</span>
          )}
        </div>
      ),
    },
    {
      key: 'total', label: 'Total kW', align: 'num', width: 120,
      sort: (r) => r.total_kw,
      render: (r) => <Num value={r.total_kw} why={r.note} />,
    },
    {
      key: 'delta', label: 'Δ vs prior', align: 'num', width: 110,
      sort: (r) => r.delta_total,
      render: (r) => <Delta value={r.delta_total} unit=" kW" />,
    },
    {
      key: 'it', label: 'IT (AC) kW', align: 'num', width: 120,
      sort: (r) => r.it_ac_kw,
      render: (r) => <Num value={r.it_ac_kw} why={r.note} />,
    },
    {
      key: 'itdc', label: 'IT (DC) kW', align: 'num', width: 120,
      render: () => <Tip className="dash" tip="no DC bus is metered in this estate">—</Tip>,
    },
    {
      key: 'cooling', label: 'Cooling kW', align: 'num', width: 120,
      sort: (r) => r.cooling_kw,
      render: (r) => <Num value={r.cooling_kw} why={r.note} />,
    },
    {
      key: 'other', label: 'Other kW', align: 'num', width: 110,
      sort: (r) => r.other_kw,
      render: (r) => <Num value={r.other_kw} why={r.note} />,
    },
    {
      key: 'pue', label: 'PUE', align: 'num', width: 90,
      sort: (r) => r.pue,
      render: (r) => (
        <Num value={r.pue} digits={3}
             why={r.it_ac_kw ? 'no total to divide' : 'no IT load in this scope to divide by'} />
      ),
    },
  ];

  function exportCsv() {
    downloadCsv(
      stampedName('power', data?.window.label ?? mode),
      ['scope', 'name', 'site', 'floor', 'total_kw', 'it_ac_kw', 'it_dc_kw',
       'cooling_kw', 'other_kw', 'pue', 'delta_total_kw', 'note'],
      t.filtered.map((r) => [
        r.kind, r.name, r.site_code, r.floor ?? '', r.total_kw ?? '', r.it_ac_kw ?? '',
        '', r.cooling_kw ?? '', r.other_kw ?? '', r.pue ?? '', r.delta_total ?? '',
        r.note ?? '',
      ]),
    );
  }

  const totals = data?.totals;
  return (
    <div className="estate">
      <PageHead
        title="Power"
        sub={<>Metered draw, split the way PUE needs it. PDUs are excluded from every
          total so the servers behind them are not counted twice.{' '}
          <Link to="/analytics">Redundancy census and single-fed loads →</Link></>}
        kpis={[
          { caption: 'Total', value: totals?.total_kw ?? null, unit: 'kW' },
          { caption: 'IT (AC)', value: totals?.it_ac_kw ?? null, unit: 'kW' },
          { caption: 'Cooling', value: totals?.cooling_kw ?? null, unit: 'kW' },
          { caption: 'PUE', value: totals?.pue ?? null, digits: 3,
            tone: (totals?.pue ?? 0) > 1.6 ? 'warn' : 'ok',
            why: 'no IT load reported, so there is nothing to divide by' },
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
        <Seg label="Reading" value={mode} onChange={setMode}
             options={[{ key: 'now', label: 'NOW' },
                       { key: 'average', label: 'AVERAGE' },
                       { key: 'peak', label: 'PEAK' }]} />
        {mode !== 'now' && (
          <>
            <label className="field">
              <span>From</span>
              <input type="date" value={from} max={to}
                     onChange={(e) => setFrom(e.target.value)} />
            </label>
            <label className="field">
              <span>To</span>
              <input type="date" value={to} min={from}
                     onChange={(e) => setTo(e.target.value)} />
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
              <span className="pair"><span className="cap">Total</span>
                <span className="v"><Num value={t.selected.total_kw} /> kW</span></span>
              <span className="pair"><span className="cap">IT (AC)</span>
                <span className="v"><Num value={t.selected.it_ac_kw} /> kW</span></span>
              <span className="pair"><span className="cap">Cooling</span>
                <span className="v"><Num value={t.selected.cooling_kw} /> kW</span></span>
              <span className="pair"><span className="cap">PUE</span>
                <span className="v"><Num value={t.selected.pue} digits={3} /></span></span>
            </div>
          </div>
        )}

        <DataTable
          rows={t.visible}
          columns={columns}
          // PUE is the state worth flagging on a power row: over 1.8 the
          // facility is spending more on overhead than on the work.
          lead={(r) => (r.pue === null ? 'none' : r.pue > 1.8 ? 'critical' : r.pue > 1.5 ? 'warn' : 'ok')}
          onRowClick={(r) => (r.kind === 'site' ? t.drillInto(r) : undefined)}
          empty={isLoading ? 'Loading…'
            : error ? 'Could not load power data.'
            : 'Nothing matches this search.'}
        />

        <TableFoot total={t.filtered.length} page={t.page} pageSize={t.pageSize}
                   onPage={t.setPage} onPageSize={t.setPageSize} onCsv={exportCsv} />
      </div>

      <Notes items={[
        ...(data?.notes ?? []),
        // The one number that would otherwise look like an error: the header
        // totals the whole estate, the rows show white space.
        !t.includeFacility && data?.totals.facility?.rooms
          ? `The totals above include ${data.totals.facility.rooms} facility rooms `
            + `(plant, switchrooms, roof) drawing ${data.totals.facility.total_kw ?? 0} kW, `
            + `of which ${data.totals.facility.cooling_kw ?? 0} kW is cooling. `
            + 'Their rows are hidden; their load is not.'
          : '',
        mode === 'now'
          ? 'Now: the newest reading the ingest worker wrote for each device.'
          : `Window: ${data?.window.label ?? ''}, compared with the equally long window before it.`,
      ].filter(Boolean)} />
    </div>
  );
}
