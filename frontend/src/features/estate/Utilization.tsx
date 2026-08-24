import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type UtilRow } from '../../api/client';
import {
  Column, DataTable, Notes, Num, PageHead, ScopeTabs, TableFoot, tone,
} from '../../components/estate';
import { downloadCsv, stampedName } from '../../lib/csv';
import { useEstateTable } from './useEstateTable';

/** Utilisation: how full the estate is, and which constraint runs out first.
 *
 *  The three percentages do NOT have equal standing, so the page says so
 *  rather than lining them up as if they did. Space comes from inventory and
 *  is exact. Power is measured against a design rating where one is recorded
 *  and against summed PDU nameplate where it is not - and installed nameplate
 *  on a 2N floor is roughly twice the usable figure. Cooling is IT heat against
 *  the rated capacity of the units that report one.
 *
 *  Each cell carries its basis in the tooltip. A percentage whose denominator
 *  is a guess should never look like one that came off a nameplate.
 */

/** `tone` has a fourth state for "not measured"; the KPI band expresses that
 *  through the absent styling instead, so it takes only the three. */
function headline(pct: number | null | undefined): 'ok' | 'warn' | 'critical' | undefined {
  const t = tone(pct);
  return t === 'none' ? undefined : t;
}

function Bar({ pct }: { pct: number | null }) {
  const t = tone(pct);
  if (pct === null || pct === undefined) {
    return <span className="ubar unknown" title="nothing to measure against" />;
  }
  return (
    <span className="ubar" title={`${pct.toFixed(1)}%`}>
      <i className={t} style={{ width: `${Math.min(100, Math.max(pct, 1))}%` }} />
    </span>
  );
}

export function Utilization() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['estate-utilization'],
    queryFn: api.estateUtilization,
    refetchInterval: 60_000,
  });

  const t = useEstateTable<UtilRow>(data?.sites ?? [], data?.rooms ?? []);

  const columns: Column<UtilRow>[] = [
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
      key: 'racks', label: 'Racks', align: 'mid', width: 78,
      sort: (r) => r.rack_count ?? 0,
      render: (r) => r.rack_count ?? 0,
    },
    {
      key: 'space', label: 'Space', align: 'num', width: 150,
      sort: (r) => r.space_pct,
      render: (r) => (
        <span title={`${r.space_used_u} of ${r.space_total_u} rack U occupied`}>
          <Num value={r.space_pct} unit="%" why="no racks in this scope" />
        </span>
      ),
    },
    { key: 'spacebar', label: '', width: 90, render: (r) => <Bar pct={r.space_pct} /> },
    {
      key: 'power', label: 'Power', align: 'num', width: 150,
      sort: (r) => r.power_pct,
      render: (r) => (
        <span title={`${r.power_used_kw} kW against ${r.power_capacity_kw ?? '—'} kW · ${r.power_basis}`}>
          <Num value={r.power_pct} unit="%" why={r.power_basis} />
        </span>
      ),
    },
    { key: 'powerbar', label: '', width: 90, render: (r) => <Bar pct={r.power_pct} /> },
    {
      key: 'cooling', label: 'Cooling', align: 'num', width: 150,
      sort: (r) => r.cooling_pct,
      render: (r) => (
        <span title={`${r.cooling_used_kw} kW of IT heat against ${r.cooling_capacity_kw ?? '—'} kW rated · ${r.cooling_basis}`}>
          <Num value={r.cooling_pct} unit="%" why={r.cooling_basis} />
        </span>
      ),
    },
    { key: 'coolingbar', label: '', width: 90, render: (r) => <Bar pct={r.cooling_pct} /> },
    {
      key: 'binding', label: 'Runs out first', width: 150,
      sort: (r) => Math.max(r.space_pct ?? -1, r.power_pct ?? -1, r.cooling_pct ?? -1),
      render: (r) => {
        const known = ([['space', r.space_pct], ['power', r.power_pct],
                        ['cooling', r.cooling_pct]] as const)
          .filter(([, v]) => v !== null) as [string, number][];
        if (!known.length) return <span className="dash">—</span>;
        const [name, pct] = known.reduce((a, b) => (b[1] > a[1] ? b : a));
        return <span className={tone(pct) === 'ok' ? 'muted' : tone(pct)}>{name} · {pct.toFixed(0)}%</span>;
      },
    },
  ];

  function exportCsv() {
    downloadCsv(
      stampedName('utilization'),
      ['scope', 'name', 'site', 'floor', 'racks', 'space_pct', 'space_used_u',
       'space_total_u', 'power_pct', 'power_used_kw', 'power_capacity_kw',
       'power_basis', 'cooling_pct', 'cooling_used_kw', 'cooling_capacity_kw',
       'cooling_basis'],
      t.filtered.map((r) => [
        r.kind, r.name, r.site_code, r.floor ?? '', r.rack_count ?? '',
        r.space_pct ?? '', r.space_used_u, r.space_total_u,
        r.power_pct ?? '', r.power_used_kw, r.power_capacity_kw ?? '', r.power_basis,
        r.cooling_pct ?? '', r.cooling_used_kw, r.cooling_capacity_kw ?? '', r.cooling_basis,
      ]),
    );
  }

  const totals = data?.totals;
  return (
    <div className="estate">
      <PageHead
        title="Utilization"
        sub={<>Space, power and cooling against what is installed. Every percentage
          carries the basis of its denominator.{' '}
          <Link to="/analytics">Constraint detail and forecast →</Link></>}
        kpis={[
          { caption: 'Space used', value: totals?.space_pct ?? null, unit: '%',
            tone: headline(totals?.space_pct) },
          { caption: 'IT load', value: totals?.power_used_kw ?? null, unit: 'kW' },
          { caption: 'Cooling installed', value: totals?.cooling_capacity_kw ?? null, unit: 'kW',
            why: 'no cooling unit reports a rated capacity' },
          { caption: 'Racks', value: totals ? String(totals.racks) : null },
        ]}
      />

      <div className="estate-tools">
        <ScopeTabs scope={t.scope} onChange={t.setScope} />
        <input className="grow" type="search" placeholder="Search sites and rooms"
               aria-label="Search" value={t.search}
               onChange={(e) => t.setSearch(e.target.value)} />
      </div>

      <div className="estate-panel">
        {t.selected && (
          <div className="estate-selected">
            <button className="back" onClick={t.clearDrill}>← All sites</button>
            <span className="who">{t.selected.name}</span>
            <div className="pairs">
              <span className="pair"><span className="cap">Space</span>
                <span className="v"><Num value={t.selected.space_pct} unit="%" /></span></span>
              <span className="pair"><span className="cap">Power</span>
                <span className="v"><Num value={t.selected.power_pct} unit="%" /></span></span>
              <span className="pair"><span className="cap">Cooling</span>
                <span className="v"><Num value={t.selected.cooling_pct} unit="%" /></span></span>
            </div>
          </div>
        )}

        <DataTable
          rows={t.visible}
          columns={columns}
          lead={(r) => tone(Math.max(r.space_pct ?? -1, r.power_pct ?? -1, r.cooling_pct ?? -1))}
          onRowClick={(r) => (r.kind === 'site' ? t.drillInto(r) : undefined)}
          empty={isLoading ? 'Loading…'
            : error ? 'Could not load utilisation.'
            : 'Nothing matches this search.'}
        />

        <TableFoot total={t.filtered.length} page={t.page} pageSize={t.pageSize}
                   onPage={t.setPage} onPageSize={t.setPageSize} onCsv={exportCsv} />
      </div>

      <Notes items={data?.notes ?? []} />
    </div>
  );
}
