import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { api, type AnomalyIncident } from '../../api/client';
import { Pagination } from '../../components/Pagination';
import { Notice } from '../../components/Notice';
import { relativeTime } from '../../lib/format';
import '../devices/devices.css';
import './incidents.css';

/** Every incident on record, open ones first. The home rail shows the week;
 *  this is the archive behind it, so a handover can look back past it and
 *  a post-incident review can find the one it wants by site, cause or
 *  words. Filters narrow, the table pages, each row opens the page. */

const DAY_MS = 24 * 3600 * 1000;
const SIZES = [25, 50, 100, 200];
const TIERS = ['power', 'plant', 'loop', 'unit', 'zone', 'server', 'room'];
const SEV_RANK: Record<string, number> = { CRITICAL: 0, MAJOR: 1, MINOR: 2, WARNING: 3, INFO: 4 };
const sevClass = (s?: string | null) => {
  const w = (s ?? '').toLowerCase();
  return w === 'critical' || w === 'major' ? w : w === 'minor' || w === 'warning' ? 'warn' : '';
};
const CAUSE_WORD: Record<string, string> = {
  power: 'power', plant: 'cooling plant', loop: 'cooling loop', unit: 'cooling unit',
  zone: 'airflow', server: 'equipment', room: 'airflow',
};
const ms = (iso: string | null | undefined) => (iso ? new Date(iso).getTime() : NaN);

function duration(fromMs: number, toMs: number): string {
  const s = Math.max(0, Math.round((toMs - fromMs) / 1000));
  if (s < 60) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h ${m - h * 60} min`;
  const d = Math.floor(h / 24);
  return `${d} d ${h - d * 24} h`;
}
function stamp(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return `${d.toLocaleDateString(undefined, { day: '2-digit', month: 'short' })} ${d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false })}`;
}

type StateFilter = 'all' | 'open' | 'resolved';
type Sort = 'recent' | 'longest' | 'biggest';

export function IncidentList() {
  const [params, setParams] = useSearchParams();
  const state = (['open', 'resolved'].includes(params.get('state') ?? '') ? params.get('state') : 'all') as StateFilter;
  const site = params.get('site') ?? '';
  const cause = params.get('cause') ?? '';
  const text = params.get('q') ?? '';
  const sort = (['longest', 'biggest'].includes(params.get('sort') ?? '') ? params.get('sort') : 'recent') as Sort;
  const page = Math.max(1, Number(params.get('page') ?? '1') || 1);
  const pageSize = SIZES.includes(Number(params.get('rows'))) ? Number(params.get('rows')) : SIZES[0];
  const set = (k: string, v: string) => setParams((p) => {
    if (v) p.set(k, v); else p.delete(k);
    if (k !== 'page') p.delete('page');       // a new filter starts at page 1
    return p;
  }, { replace: true });

  // The page itself: the server filters, orders and pages, and says how
  // many match, so a pager can reach any page without fetching the rest.
  const states: ('open' | 'resolved')[] = state === 'all' ? ['open', 'resolved'] : [state];
  const list = useQuery({
    queryKey: ['anomaly-incidents', 'list', states.join(','), site, cause, text, sort, page, pageSize],
    queryFn: () => api.anomalyIncidents({
      state: states, datacenter: site || undefined, tier: cause || undefined, q: text || undefined,
      order: sort, with_total: 'true', limit: String(pageSize), offset: String((page - 1) * pageSize),
    }),
    refetchInterval: 30_000,
    retry: false,
    placeholderData: (prev) => prev,
  });
  // The band's four counts, each one page of one row with its total.
  const now = Date.now();
  const since7 = new Date(now - 7 * DAY_MS).toISOString().slice(0, 13);
  const since30 = new Date(now - 30 * DAY_MS).toISOString().slice(0, 13);
  const count = (key: string, p: Parameters<typeof api.anomalyIncidents>[0]) => useQuery({
    queryKey: ['anomaly-incidents', 'count', key],
    queryFn: async () => (await api.anomalyIncidents({ ...p, with_total: 'true', limit: '1' })).total ?? 0,
    refetchInterval: 60_000,
    retry: false,
  });
  /* eslint-disable react-hooks/rules-of-hooks -- four fixed calls, never conditional */
  const nOpenQ = count('open', { state: 'open' });
  const in7Q = count(`r7-${since7}`, { state: 'resolved', resolved_since: `${since7}:00:00Z` });
  const in30Q = count(`r30-${since30}`, { state: 'resolved', resolved_since: `${since30}:00:00Z` });
  const allQ = count('all', { state: ['open', 'resolved'] });
  /* eslint-enable react-hooks/rules-of-hooks */
  const sites = useQuery({ queryKey: ['sites-overview'], queryFn: api.sitesOverview, staleTime: 300_000 });

  const loading = list.isLoading;
  const rows: AnomalyIncident[] = list.data?.items ?? [];
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const worstOpen = rows.filter((i) => i.state === 'open')
    .map((i) => i.evidence.worst ?? 'INFO').sort((x, y) => (SEV_RANK[x] ?? 9) - (SEV_RANK[y] ?? 9))[0];
  const nOpen = nOpenQ.data ?? 0;
  const siteCodes = (sites.data?.sites ?? []).map((r) => r.code).sort();
  const tile = (q: { data?: number; isLoading: boolean }) =>
    q.isLoading ? <span className="dv-skel">0</span> : (q.data ?? 0);

  return (
    <div className="dv-page il-page">
      <nav className="dv-crumb" aria-label="Where this is">
        <Link to="/">Home</Link><span aria-hidden>›</span><span>Incidents</span>
      </nav>
      <header className={`dv-head${nOpen > 0 && worstOpen ? ` sev-${worstOpen.toLowerCase()}` : ''}`}>
        <div className="dv-title">
          <h2>Incidents</h2>
          <p className="subtitle">One cause and everything it did, open ones first. The home rail shows the week; this is all of it.</p>
        </div>
        <div className="dv-band">
          <div className={`dv-tile no-glyph${nOpen > 0 && worstOpen ? ` tone-${sevClass(worstOpen)}` : ''}`}>
            <b>{tile(nOpenQ)}</b><span>open now</span>
          </div>
          <div className="dv-tile no-glyph"><b>{tile(in7Q)}</b><span>resolved, 7 d</span></div>
          <div className="dv-tile no-glyph"><b>{tile(in30Q)}</b><span>resolved, 30 d</span></div>
          <div className="dv-tile no-glyph"><b>{tile(allQ)}</b><span>on record</span></div>
        </div>
      </header>

      {list.isError && <Notice tone="error">Incidents could not be read.</Notice>}

      <section className="dv-card">
        <div className="ip-filters">
          <label>State
            <select value={state} onChange={(e) => set('state', e.target.value === 'all' ? '' : e.target.value)}>
              <option value="all">any</option><option value="open">open</option><option value="resolved">resolved</option>
            </select>
          </label>
          <label>Site
            <select value={site} onChange={(e) => set('site', e.target.value)}>
              <option value="">any</option>{siteCodes.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </label>
          <label>Cause
            <select value={cause} onChange={(e) => set('cause', e.target.value)}>
              <option value="">any</option>{TIERS.map((c) => <option key={c} value={c}>{CAUSE_WORD[c] ?? c}</option>)}
            </select>
          </label>
          <label>Order
            <select value={sort} onChange={(e) => set('sort', e.target.value === 'recent' ? '' : e.target.value)}>
              <option value="recent">most recent</option><option value="longest">longest</option><option value="biggest">most alarms</option>
            </select>
          </label>
          <label className="ip-search">Title, root or room
            <input type="search" value={text} onChange={(e) => set('q', e.target.value)} placeholder="CHL2" />
          </label>
        </div>

        {loading ? (
          <div className="table-frame"><table className="ip-table il-table"><tbody>
            {[0, 1, 2, 3, 4].map((n) => (
              <tr key={n}><td colSpan={8}><span className="dv-skel">An incident title of the usual length, with its numbers after</span></td></tr>
            ))}
          </tbody></table></div>
        ) : rows.length === 0 ? (
          <p className="dv-empty">{(allQ.data ?? 0) === 0 ? 'No incident on record yet. Every alarm so far has stood on its own.' : 'No incident matches these filters.'}</p>
        ) : (
          <div className="table-frame">
            <table className="ip-table il-table">
              <thead>
                <tr>
                  <th>Site</th><th>Incident</th><th>Cause</th><th>State</th><th>Worst</th>
                  <th className="num">Alarms</th><th className="num">Rooms</th><th>Opened</th><th>Lasted</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((i) => {
                  const isOpen = i.state === 'open';
                  const w = i.evidence.worst ?? '';
                  return (
                    <tr key={i.id} className={isOpen ? 'is-open' : 'is-done'}>
                      <td className="il-site" title={i.site_name ?? undefined}>{i.site_code ?? '—'}</td>
                      <td className="il-title">
                        <Link to={`/incidents/${i.id}`}>{i.title}</Link>
                        <span className="il-sub">{i.room_name ?? ''}{i.room_name && i.root_device_name ? ' · ' : ''}{i.root_device_name ?? ''}</span>
                      </td>
                      <td>{CAUSE_WORD[i.tier] ?? i.tier}</td>
                      <td><span className={`chip ip-state ${isOpen ? 'is-open' : 'is-done'}`}><span className="dot" aria-hidden />{isOpen ? 'open' : 'resolved'}</span></td>
                      <td><span className={`ip-sev ${sevClass(w)}`}>{w.toLowerCase() || '—'}</span></td>
                      <td className="num">{i.alarm_count}</td>
                      <td className="num">{i.evidence.by_room?.length ?? (i.room_id ? 1 : 0)}</td>
                      <td className="num" title={i.opened_at}>{stamp(i.opened_at)}</td>
                      <td className="num">
                        {duration(ms(i.opened_at), i.resolved_at ? ms(i.resolved_at) : now)}
                        {isOpen && <span className="muted"> · last {relativeTime(i.last_activity_at)}</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {!loading && (
          <Pagination page={Math.min(page, pages)} pageSize={pageSize} shown={rows.length} total={total}
                      hasNext={page < pages} noun="incidents"
                      onPage={(n) => set('page', n <= 1 ? '' : String(n))}
                      onSize={(n) => set('rows', n === SIZES[0] ? '' : String(n))} />
        )}
      </section>
    </div>
  );
}
