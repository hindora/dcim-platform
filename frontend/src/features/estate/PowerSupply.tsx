/** The electrical chain, read as supplies rather than as loads.
 *
 *  The rest of the Power page answers "what is the estate drawing". This
 *  answers the question underneath it: is the feed intact, and what is one
 *  failure from dark. Utility, transformer, generators, switchgear, transfer
 *  switches, UPS, motor control centres, mechanical panels and power panels,
 *  each read by the same readers and judged by the same verdicts the Thermal
 *  page's facility rooms use, with the chain's load laid on top.
 *
 *  Two censuses, not one. IT loads are counted by cords - two sides, one, or
 *  none - because a server on one cord is the finding a maintenance window
 *  turns on. Plant is counted in GROUPS - the chillers of a site, the CRAHs
 *  of a hall - on how many members still have a feed, because a chiller is
 *  fed from one MCC through one breaker by design and its redundancy is the
 *  spare machine beside it, not a second cord. Counting fifty plant machines
 *  as "loads without full redundancy" buried the three servers that were.
 *
 *  A power panel has no protocol of its own. It is read through the meter
 *  clamped on it, and the row says so; a meter that has gone quiet makes the
 *  panel silent rather than fine.
 */

import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type PlantGroup, type PowerFleet, type SupplyRow } from '../../api/client';
import { Column, DataTable, Notes, Num, Seg, TableFoot } from '../../components/estate';
import { Tip } from '../../components/HoverTip';
import { downloadCsv, stampedName } from '../../lib/csv';
import { TYPE_WORD, Verdict, verdictTone } from './Plant';

export function useSupply(siteId?: string | null, enabled = true) {
  return useQuery<PowerFleet>({
    queryKey: ['power-fleet', siteId ?? ''],
    queryFn: () => api.powerFleet(siteId ?? undefined),
    enabled,
    refetchInterval: 60_000,
    placeholderData: (prev) => prev,
  });
}

const CENSUS_TONE: Record<string, string> = { '2N': 'ok', single_feed: 'warn', no_feed: 'critical' };
const CENSUS_WORD: Record<string, string> = {
  '2N': 'on both sides', single_feed: 'on one cord', no_feed: 'fed by nothing',
};
const GROUP_TONE: Record<string, 'ok' | 'warn' | 'critical'> = { ok: 'ok', tight: 'warn', short: 'critical' };

/** IT loads by cords, as a partition of the whole. */
function ItCensus({ census }: { census: Record<string, number> }) {
  const total = Object.values(census).reduce((a, b) => a + b, 0);
  if (!total) return <p className="muted">No IT load is fed by anything in this scope.</p>;
  const order = ['2N', 'single_feed', 'no_feed'];
  return (
    <div className="pw-census">
      <div className="stack-bar" role="img"
           aria-label={order.map((k) => `${census[k] ?? 0} ${CENSUS_WORD[k]}`).join(', ')}>
        {order.filter((k) => (census[k] ?? 0) > 0).map((k) => (
          <div key={k} className={`stack-seg ${CENSUS_TONE[k]}`}
               style={{ width: `${(100 * (census[k] ?? 0)) / total}%` }} />
        ))}
      </div>
      <div className="legend">
        {order.map((k) => (
          <span key={k}><i className={CENSUS_TONE[k]} />
            <b>{(census[k] ?? 0).toLocaleString()}</b> {CENSUS_WORD[k]}
            <span className="muted"> ({((100 * (census[k] ?? 0)) / total).toFixed(0)}%)</span>
          </span>
        ))}
      </div>
    </div>
  );
}

/** Plant groups: each a chip, the short ones first and coloured. */
function PlantGroups({ groups }: { groups: PlantGroup[] }) {
  if (!groups.length) return <p className="muted">No plant is fed by anything in this scope.</p>;
  return (
    <div className="pw-groups">
      {groups.map((g) => (
        <Tip key={`${g.room_id}-${g.device_type}`}
             tip={<>{g.why}{g.unfed.length ? <><br />unfed: {g.unfed.join(', ')}</> : null}</>}>
          <span className={`pw-group ${GROUP_TONE[g.verdict]}`}>
            <span className="where">{g.site_code} · {g.room_name}</span>
            <span className="what">{g.n} × {TYPE_WORD[g.device_type] ?? g.device_type}</span>
            <span className="fed">{g.fed}/{g.n} fed</span>
          </span>
        </Tip>
      ))}
    </div>
  );
}

function stateCell(s: SupplyRow): React.ReactNode {
  if (s.load_source === 'metered_by' && !s.state_label) {
    return s.metered_stale
      ? <Tip tip={`${s.metered_by} has not reported in 15 minutes`}><span className="warn">Meter stale</span></Tip>
      : <Tip tip={`no protocol of its own; read through ${s.metered_by}`}><span className="muted">Metered</span></Tip>;
  }
  if (!s.state_label) {
    return <Tip className="dash" tip={s.monitored ? 'no state inside the last-known window' : 'no monitoring endpoint - in inventory, not in telemetry'}>—</Tip>;
  }
  const bad = ['On battery', 'Bypass', 'On generator', 'No source', 'Dead', 'Lost'].includes(s.state_label);
  return <span className={bad ? 'critical' : undefined}>{s.state_label}</span>;
}

/** What else the row knows, for the name's tooltip. */
function extras(s: SupplyRow): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  const push = (k: string, body: React.ReactNode) => out.push(<span className="spread-line" key={k}>{body}</span>);
  if (s.model) push('model', <span className="muted">{s.model}</span>);
  if (s.voltage_v !== null) push('v', <>{s.voltage_v.toFixed(0)} V</>);
  if (s.power_factor !== null) push('pf', <>PF <b>{s.power_factor.toFixed(2)}</b></>);
  if (s.thd_pct !== null) push('thd', <>voltage THD <b>{s.thd_pct.toFixed(1)} %</b></>);
  if (s.battery_minutes !== null) push('rt', <>runtime <b>{s.battery_minutes.toFixed(0)} min</b> at this load</>);
  if (s.battery_health_pct !== null) push('bh', <>battery health <b>{s.battery_health_pct.toFixed(0)} %</b></>);
  if (s.battery_c !== null) push('bc', <>battery <b>{s.battery_c.toFixed(1)} °C</b></>);
  if (s.fuel_pct !== null) push('fuel', <>fuel <b>{s.fuel_pct.toFixed(0)} %</b></>);
  if (s.run_minutes !== null && s.run_minutes > 0) push('run', <>running <b>{s.run_minutes.toFixed(0)} min</b></>);
  if (s.coolant_c !== null) push('cool', <>coolant <b>{s.coolant_c.toFixed(0)} °C</b></>);
  if (s.transfers !== null) push('xfer', <><b>{s.transfers}</b> transfers</>);
  if (s.oil_c !== null) push('oil', <>oil <b>{s.oil_c.toFixed(1)} °C</b>{s.winding_c !== null ? <>, winding <b>{s.winding_c.toFixed(1)} °C</b></> : null}</>);
  if (s.metered_by) push('m', <>read through <b>{s.metered_by}</b></>);
  if (!out.length) push('none', <span className="muted">nothing more reported</span>);
  return out;
}

export function PowerSupply({ siteId, onSite, sites }: {
  siteId: string | null;
  onSite: (id: string | null) => void;
  sites: { id: string; code: string }[];
}) {
  const { data, error, isLoading } = useSupply(siteId);
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(50);

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    const all = data?.supplies ?? [];
    return q ? all.filter((s) => `${s.name} ${s.device_type} ${s.room_name ?? ''} ${s.site_code}`.toLowerCase().includes(q)) : all;
  }, [data, search]);
  const visible = rows.slice(page * pageSize, (page + 1) * pageSize);

  const columns: Column<SupplyRow>[] = [
    {
      key: 'name', label: 'Supply', width: 200,
      help: 'The device, as inventory names it. The tip carries what else it reports.',
      sort: (s) => s.name,
      render: (s) => <Tip tip={<>{extras(s)}</>}><b>{s.name}</b></Tip>,
    },
    {
      key: 'type', label: 'Type', width: 150,
      sort: (s) => s.device_type,
      render: (s) => <span className="muted">{TYPE_WORD[s.device_type] ?? s.device_type}</span>,
    },
    {
      key: 'where', label: 'Where', width: 170,
      help: 'Site and room it stands in.',
      sort: (s) => `${s.site_code} ${s.room_name ?? ''}`,
      render: (s) => <span className="muted">{s.site_code} · {s.room_name ?? '—'}</span>,
    },
    {
      key: 'state', label: 'State', align: 'mid', width: 110,
      help: 'From the device\'s own binaries, never guessed from watts: on mains, on battery, energised, on generator. A power panel has none and is read through its meter.',
      sort: (s) => s.state_label ?? (s.load_source === 'metered_by' ? 'Metered' : null),
      render: stateCell,
    },
    {
      key: 'pct', label: 'Load', align: 'num', width: 84,
      help: 'Load as a share of the device\'s rating. Amber from 80 %, red from 90 %.',
      sort: (s) => s.load_pct,
      render: (s) => (
        <Tip tip={s.load_pct === null ? 'no load figure and no rating to divide it by'
          : <>{s.load_kw?.toFixed(1)} kW of {s.rated_kw?.toFixed(0) ?? '?'} kW rated</>}>
          <span className={(s.load_pct ?? 0) >= 90 ? 'critical' : (s.load_pct ?? 0) >= 80 ? 'warn' : undefined}>
            <Num value={s.load_pct} unit="%" digits={0} />
          </span>
        </Tip>
      ),
    },
    {
      key: 'kw', label: 'kW', align: 'num', width: 90,
      help: 'What passes through it. Measured by its own meter, derived from what it feeds, or read through a clamp-on meter - the Source column says which.',
      sort: (s) => s.load_kw,
      render: (s) => <Num value={s.load_kw} why="nothing downstream reports and it meters nothing itself" />,
    },
    {
      key: 'source', label: 'Source', width: 120,
      help: 'measured: the device\'s own meter. derived: the sum of what it feeds, halved for dual-corded loads. metered by: a power panel read through the Verdigris meter clamped on it.',
      sort: (s) => s.load_source,
      render: (s) => (s.load_source === 'metered_by'
        ? <Tip tip={s.metered_by ?? ''}><span className="muted">metered by</span></Tip>
        : s.load_source
          ? <span className="muted">{s.load_source}</span>
          : <span className="dash">—</span>),
    },
    {
      key: 'imb', label: 'Imbalance', align: 'num', width: 96,
      help: 'Phase imbalance the device reports. NEMA derates a motor above 1 %; 3 % is the line worth a look on a board feeding pumps and fans.',
      sort: (s) => s.imbalance_pct,
      render: (s) => (s.imbalance_pct === null
        ? <Tip className="dash" tip="reports no phase figures">—</Tip>
        : <span className={s.imbalance_pct > 3 ? 'warn' : undefined}><Num value={s.imbalance_pct} unit="%" /></span>),
    },
    {
      key: 'alarms', label: 'Alarms', align: 'num', width: 80,
      help: 'Open conditions on this device, every category.',
      sort: (s) => s.alarms_open,
      render: (s) => (s.alarms_open ? <span className="warn">{s.alarms_open}</span> : <span className="dash">0</span>),
    },
    {
      key: 'verdict', label: 'Verdict', width: 140,
      help: 'What is true about it and what to do: the plant readers\' finding first (on battery, on bypass, dead bus, low fuel), then the chain\'s (near rating, read through a stale meter).',
      sort: (s) => s.verdict,
      render: (s) => <Verdict v={s.verdict} why={s.why} />,
    },
  ];

  function exportCsv() {
    downloadCsv(
      stampedName('power-supplies', siteId ?? 'estate'),
      ['name', 'type', 'site', 'room', 'status', 'state', 'load_pct', 'load_kw', 'rated_kw',
       'source', 'metered_by', 'imbalance_pct', 'voltage_v', 'power_factor', 'battery_minutes',
       'battery_health_pct', 'fuel_pct', 'open_alarms', 'verdict', 'why'],
      rows.map((s) => [
        s.name, s.device_type, s.site_code, s.room_name ?? '', s.status, s.state_label ?? '',
        s.load_pct ?? '', s.load_kw ?? '', s.rated_kw ?? '', s.load_source ?? '', s.metered_by ?? '',
        s.imbalance_pct ?? '', s.voltage_v ?? '', s.power_factor ?? '', s.battery_minutes ?? '',
        s.battery_health_pct ?? '', s.fuel_pct ?? '', s.alarms_open, s.verdict, s.why ?? '',
      ]),
    );
  }

  const siteOptions = [{ key: 'all', label: 'ALL SITES' },
                       ...sites.map((s) => ({ key: s.id, label: s.code }))];

  return (
    <>
      <div className="estate-tools">
        <Seg label="Site" value={siteId ?? 'all'}
             options={siteOptions}
             onChange={(k) => onSite(k === 'all' ? null : k)} />
        <input className="grow" type="search" placeholder="Search supplies"
               aria-label="Search supplies" value={search}
               onChange={(e) => { setSearch(e.target.value); setPage(0); }} />
      </div>

      <div className="estate-panel pw-census-panel">
        <div className="pw-census-cols">
          <div>
            <h3>IT loads, by cords</h3>
            {data ? <ItCensus census={data.redundancy_census} /> : <p className="muted">Loading…</p>}
            {data && data.at_risk_total > 0 && (
              <p className="pw-at-risk">
                {data.at_risk_total} at risk:{' '}
                {data.at_risk.slice(0, 6).map((a) => (
                  <Tip key={a.device_id} tip={a.reason}><span className={a.redundancy === 'no_feed' ? 'critical' : 'warn'}>{a.name}</span></Tip>
                )).reduce<React.ReactNode[]>((acc, el, i) => (i ? [...acc, ', ', el] : [el]), [])}
                {data.at_risk_total > 6 ? ` and ${data.at_risk_total - 6} more` : ''}
              </p>
            )}
          </div>
          <div>
            <h3>Plant groups, by members fed{data && data.plant_groups_short > 0 && <span className="count"> {data.plant_groups_short} short</span>}</h3>
            {data ? <PlantGroups groups={data.plant_groups} /> : <p className="muted">Loading…</p>}
          </div>
        </div>
      </div>

      <div className="estate-panel">
        <DataTable
          rows={visible}
          columns={columns}
          lead={(s) => verdictTone(s.verdict)}
          empty={isLoading ? 'Loading…' : error ? 'Could not load the supplies.' : 'Nothing matches this search.'}
        />
        <TableFoot total={rows.length} page={page} pageSize={pageSize} noun="supplies"
                   onPage={setPage} onPageSize={(n) => { setPageSize(n); setPage(0); }} onCsv={exportCsv} />
      </div>

      <Notes items={[
        'Dual-fed means a path from a source reaches the load without passing through the failed element - not merely two cords. Two cords into the same strip, or two strips on one side, is one feed wearing a disguise.',
        'Plant is single-corded by design: a chiller is fed from one MCC through one breaker, and its redundancy is the spare machine beside it. A group is tight when one member has lost its feed and short when more have.',
        'A power panel has no protocol of its own. Its load is the Verdigris meter clamped on it; the panel reads silent when that meter has not reported in fifteen minutes.',
        'Derived load is the sum of what a device feeds, halved for a dual-corded load, which is what the metering on a real 2N pair shows until one side fails.',
      ]} />
    </>
  );
}
