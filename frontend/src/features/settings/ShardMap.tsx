import { Link, useSearchParams } from 'react-router-dom';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import {
  api,
  type AssignmentMove,
  type ShardMapPage,
  type ShardRow,
} from '../../api/client';
import { humanise, oneLine, relativeTime } from '../../lib/format';
import { Tip } from '../../components/HoverTip';
import { Pagination } from '../../components/Pagination';
import { KpiBand, type Kpi } from '../../components/estate';

/** The shard map (docs/26 Phase 5): which collector polls every endpoint,
 *  and every move it made.
 *
 *  Two owners per row, on purpose. "Polled by" is what the serving path
 *  hands each collector right now; "recorded" is what the assigner last
 *  wrote, with the epoch, time and reason of the move. The serving path
 *  reads the record, so they agree except for the seconds between a change
 *  (a drain, a pin, a retired owner) and the assigner's next tick - and
 *  persistently only where no assigner runs. When they disagree the page
 *  says so instead of picking one.
 *
 *  Filters live in the URL, so "every BMS endpoint on col-2" is a link an
 *  engineer can send. Drains are started from the Collectors page, where
 *  the preview is; this page is where you watch them land. */
export function ShardMap() {
  const [params, setParams] = useSearchParams();
  const get = (k: string) => params.get(k) ?? '';
  const page = Math.max(1, Number(params.get('page') || 1));
  const pageSize = Number(params.get('page_size') || 50);
  const history = params.get('endpoint');

  const set = (patch: Record<string, string | null>, keepPage = false) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v == null || v === '') next.delete(k); else next.set(k, v);
    }
    if (!keepPage) next.delete('page');
    setParams(next, { replace: true });
  };

  const q = useQuery<ShardMapPage>({
    queryKey: ['shard-map', params.toString()],
    queryFn: () => api.shardMap({
      collector_id: get('collector') || undefined,
      pool_id: get('pool') || undefined,
      protocol: get('protocol') || undefined,
      q: get('q') || undefined,
      disagree_only: get('show') === 'disagree',
      unassigned_only: get('show') === 'unassigned',
      limit: pageSize,
      offset: (page - 1) * pageSize,
    }),
    placeholderData: keepPreviousData,
    refetchInterval: 15_000,
  });

  if (q.isLoading) return <p className="muted">Loading…</p>;
  if (q.error || !q.data) return <div className="banner">Could not load the shard map.</div>;
  const { summary, items, total } = q.data;
  const moves = Object.values(summary.moves_24h).reduce((a, b) => a + b, 0);

  const kpis: Kpi[] = [
    { caption: 'Endpoints', value: summary.total, digits: 0 },
    { caption: 'Polled by nobody', value: summary.unassigned, digits: 0,
      tone: summary.unassigned ? 'critical' : 'ok' },
    { caption: 'Record disagrees', value: summary.record_disagrees, digits: 0,
      tone: summary.record_disagrees ? 'warn' : 'ok' },
    { caption: 'Not recorded', value: summary.unrecorded, digits: 0,
      why: summary.unrecorded === summary.total && summary.total > 0
        ? 'No assigner has written a record yet - is the ingest worker running this release?'
        : null },
    { caption: 'Moves, 24 h', value: moves, digits: 0 },
  ];

  return (
    <div className="stack">
      <div>
        <h2>Shard map</h2>
        <p className="subtitle">
          <Tip tip={oneLine(`"Polled by" is who is given the endpoint right now.
                  "Recorded" is the assigner's last written move, which is what
                  collectors are served. They differ only until the assigner's
                  next tick after a change, or where no assigner is running.`)}>
            Which collector polls every device, and every move it made.
          </Tip>
        </p>
      </div>

      <KpiBand items={kpis} />

      <div className="table-frame">
        <table>
          <thead>
            <tr><th>Collector</th><th>Pool</th><th>State</th>
                <th className="num">Polls</th><th className="num">Pinned</th><th /></tr>
          </thead>
          <tbody>
            {summary.by_collector.map((c) => (
              <tr key={c.collector_id}>
                <td className="mono">
                  {c.not_in_fleet ? c.collector_id : (
                    <Link to={`/settings/collectors/${encodeURIComponent(c.collector_id)}`}>
                      {c.collector_id}
                    </Link>
                  )}
                </td>
                <td className="muted">
                  {summary.pools.find((p) => p.id === c.pool_id)?.name ?? '—'}
                </td>
                <td>
                  {c.not_in_fleet ? (
                    <Tip className="critical" tip={oneLine(`Endpoints are pinned to a collector
                            that is not running or not approved. The pin is kept on
                            purpose, and nothing polls them meanwhile.`)}>
                      not in fleet
                    </Tip>
                  ) : !c.healthy ? <span className="warn">silent</span>
                    : !c.accepting ? <span className="muted">draining</span>
                      : <span className="ok">active</span>}
                </td>
                <td className="num">{c.owned}</td>
                <td className="num muted">{c.pinned || '—'}</td>
                <td>
                  <button onClick={() => set({ collector: c.collector_id })}>Show</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <input placeholder="Device or address" value={get('q')} style={{ minWidth: 200 }}
               onChange={(e) => set({ q: e.target.value })} />
        <select value={get('collector')} onChange={(e) => set({ collector: e.target.value })}
                aria-label="Collector">
          <option value="">Every collector</option>
          {summary.by_collector.map((c) => (
            <option key={c.collector_id} value={c.collector_id}>{c.collector_id}</option>
          ))}
        </select>
        <select value={get('pool')} onChange={(e) => set({ pool: e.target.value })}
                aria-label="Pool">
          <option value="">Every pool</option>
          {summary.pools.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <select value={get('protocol')} onChange={(e) => set({ protocol: e.target.value })}
                aria-label="Protocol">
          <option value="">Every protocol</option>
          {summary.protocols.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <select value={get('show')} onChange={(e) => set({ show: e.target.value })}
                aria-label="Show">
          <option value="">All endpoints</option>
          <option value="unassigned">Polled by nobody</option>
          <option value="disagree">Record disagrees</option>
        </select>
        {[...params.keys()].some((k) => ['q', 'collector', 'pool', 'protocol', 'show'].includes(k)) && (
          <button onClick={() => setParams(new URLSearchParams(), { replace: true })}>Clear</button>
        )}
      </div>

      <div className="table-frame">
        <table>
          <thead>
            <tr>
              <th>Device</th><th>Protocol</th><th>Address</th><th>Pool</th>
              <th>Polled by</th>
              <th>
                <Tip tip="The assigner's last written move: who, when, why.">Recorded</Tip>
              </th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.id} onClick={() => set({ endpoint: r.id }, true)}
                  style={{ cursor: 'pointer' }}>
                <td><Link to={`/devices/${r.device_id}`}
                          onClick={(e) => e.stopPropagation()}>{r.device_name}</Link></td>
                <td className="muted">{r.protocol}</td>
                <td className="mono muted">{r.address ?? '—'}</td>
                <td className="muted">{r.pool_name ?? (r.pool_id ? r.pool_id.slice(0, 8) : '—')}</td>
                <td>
                  {r.owner ? <span className="mono">{r.owner}</span>
                    : <span className="critical">nobody</span>}
                  {r.pinned && <Tip className="muted" tip="Pinned: the operator's choice beats the hash."> · pinned</Tip>}
                </td>
                <td className={r.record_disagrees ? 'warn' : 'muted'}>
                  <Recorded r={r} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {items.length === 0 && <p className="muted">No endpoints match.</p>}
      {(items.length > 0 || page > 1) && (
        <Pagination page={page} pageSize={pageSize} shown={items.length} total={total}
                    hasNext={page * pageSize < total} noun="endpoints"
                    onPage={(n) => set({ page: String(n) }, true)}
                    onSize={(n) => set({ page_size: String(n) })} />
      )}

      {history && (
        <HistorySheet row={items.find((r) => r.id === history)} endpointId={history}
                      onClose={() => set({ endpoint: null }, true)} />
      )}
    </div>
  );
}

function Recorded({ r }: { r: ShardRow }) {
  if (!r.reason) return <>not recorded</>;
  return (
    <>
      {r.record_disagrees && <>{r.recorded_owner ?? 'nobody'} · </>}
      {humanise(r.reason)} {relativeTime(r.since)}
      <span className="muted"> · epoch {r.epoch}</span>
    </>
  );
}

const REASON_TIP: Record<string, string> = {
  initial: 'First owner the assigner recorded.',
  rebalance: 'A member joined or left and the pool had drifted past the damping threshold.',
  pin: 'An operator pinned it to this collector.',
  pool_empty: 'No collector serving its pool or site was accepting work.',
  drain: 'Its previous owner was drained.',
  failover: 'Its previous owner went silent past the failover threshold (HA pools).',
  failback: 'Its original owner came back healthy and took it back.',
};

function HistorySheet({ row, endpointId, onClose }: {
  row?: ShardRow;
  endpointId: string;
  onClose: () => void;
}) {
  const h = useQuery({
    queryKey: ['assignment-history', endpointId],
    queryFn: () => api.assignmentHistory(endpointId),
  });
  const moves: AssignmentMove[] = h.data?.moves ?? [];
  return (
    <div className="sheet-scrim" role="dialog" aria-modal="true" aria-label="Ownership history"
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="sheet narrow">
        <header className="sheet-head">
          <div>
            <h2>{row ? `${row.device_name} · ${row.protocol}` : 'Ownership history'}</h2>
            <p>
              {row?.owner ? <>Polled by <span className="mono">{row.owner}</span> now.</>
                : row ? 'Polled by nobody now.' : null}
              {' '}Every recorded move, newest first.
            </p>
          </div>
          <button className="close" onClick={onClose} aria-label="Close">✕</button>
        </header>
        <div className="sheet-body">
          {h.isLoading && <p className="muted">Loading…</p>}
          {h.error && <div className="banner">Could not load its history.</div>}
          {h.data && moves.length === 0 && (
            <p className="muted">
              No move recorded. Either no assigner has run since this endpoint was
              added, or it has never had an owner.
            </p>
          )}
          {moves.length > 0 && (
            <table>
              <thead><tr><th>When</th><th>From</th><th>To</th><th>Why</th></tr></thead>
              <tbody>
                {moves.map((m) => (
                  <tr key={m.epoch + m.at}>
                    <td className="muted">{relativeTime(m.at)}</td>
                    <td className="mono">{m.from_collector ?? <span className="muted">—</span>}</td>
                    <td className="mono">{m.to_collector ?? <span className="critical">nobody</span>}</td>
                    <td>
                      <Tip tip={REASON_TIP[m.reason] ?? m.reason}>{humanise(m.reason)}</Tip>
                      <span className="muted"> · epoch {m.epoch}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="muted">
            Kept for 180 days. A move's "from" is blank for the first record, and for
            owners recorded before history was kept.
          </p>
        </div>
      </section>
    </div>
  );
}
