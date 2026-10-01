import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ApiError,
  api,
  type CollectorCommand,
  type PoolsPage,
  type Rollout,
} from '../../api/client';
import { oneLine, relativeTime } from '../../lib/format';
import { Tip } from '../../components/HoverTip';

/** Collector releases and rollouts (docs/26 Phase 7).
 *
 *  A rollout upgrades one member of each pool at a time and waits for it to
 *  come back healthy on the new build before touching the next, so a pool
 *  with two members never goes dark. A collector verifies every build against
 *  its own trusted keys before running it, and the build it replaced watches
 *  the new one: if the new build does not report healthy within its window,
 *  the old one puts itself back and the rollout stops. */
export function Releases() {
  const qc = useQueryClient();
  const releases = useQuery({ queryKey: ['releases'], queryFn: () => api.collectorReleases() });
  const rollouts = useQuery({ queryKey: ['rollouts'], queryFn: () => api.rollouts(),
                              refetchInterval: 5_000 });
  const pools = useQuery<PoolsPage>({ queryKey: ['pools'], queryFn: () => api.pools() });
  const [version, setVersion] = useState('');
  const [chosen, setChosen] = useState<string[]>([]);
  const start = useMutation({
    mutationFn: () => api.startRollout(version, chosen),
    onSuccess: () => { setChosen([]); void qc.invalidateQueries({ queryKey: ['rollouts'] }); },
  });
  const cancel = useMutation({
    mutationFn: (id: string) => api.cancelRollout(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['rollouts'] }),
  });

  if (releases.isLoading || rollouts.isLoading) return <p className="muted">Loading…</p>;
  const rels = releases.data?.releases ?? [];
  const fleet = rollouts.data?.fleet ?? [];
  const list = rollouts.data?.rollouts ?? [];
  const poolRows = pools.data?.pools ?? [];
  const single = poolRows.filter((p) => chosen.includes(p.id) && p.members.length < 2);
  const latest = rels[0]?.version;

  return (
    <div className="stack">
      <div>
        <h2>Releases</h2>
        <p className="subtitle">
          <Tip tip={oneLine(`Each collector runs only builds signed by a key in its own
                  configuration. The platform stores and serves releases; it cannot sign
                  one.`)}>
            Which build every collector runs, and upgrading them a pool at a time.
          </Tip>
        </p>
      </div>

      <fieldset className="proto">
        <legend>Fleet</legend>
        <table>
          <thead><tr><th>Collector</th><th>Pool</th><th>Running</th><th>Heartbeat</th></tr></thead>
          <tbody>
            {fleet.map((c) => (
              <tr key={c.id}>
                <td className="mono"><Link to={`/settings/collectors/${encodeURIComponent(c.id)}`}>{c.id}</Link></td>
                <td className="muted">{c.pool ?? '—'}</td>
                <td className={c.version && latest && c.version !== latest ? 'warn mono' : 'mono'}>
                  {c.version ?? '—'}
                </td>
                <td className="muted">
                  {c.heartbeat_age_s == null ? 'never'
                    : c.heartbeat_age_s < 60 ? 'healthy' : `silent ${Math.round(c.heartbeat_age_s)} s`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </fieldset>

      <fieldset className="proto">
        <legend>Signed releases</legend>
        {rels.length === 0 ? (
          <p className="muted">
            None yet. Sign and publish one with <span className="mono">scripts/publish_release.py</span>.
          </p>
        ) : (
          <table>
            <thead><tr><th>Version</th><th>Signed by</th><th className="num">Size</th><th>SHA-256</th><th>Published</th></tr></thead>
            <tbody>
              {rels.map((r) => (
                <tr key={r.version}>
                  <td className="mono">{r.version}</td>
                  <td className="mono muted">{r.key_id}</td>
                  <td className="num muted">{(r.size_bytes / 1048576).toFixed(1)} MiB</td>
                  <td className="mono muted">{r.sha256.slice(0, 16)}…</td>
                  <td className="muted">{relativeTime(r.created_at)}{r.created_by ? ` by ${r.created_by}` : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </fieldset>

      {rels.length > 0 && (
        <fieldset className="proto">
          <legend>Start a rollout</legend>
          {start.error && (
            <div className="banner">
              {start.error instanceof ApiError ? start.error.message : String(start.error)}
            </div>
          )}
          <div className="form-grid">
            <label>
              <span>Release</span>
              <select value={version} onChange={(e) => setVersion(e.target.value)}>
                <option value="">Choose…</option>
                {rels.map((r) => <option key={r.version} value={r.version}>{r.version}</option>)}
              </select>
            </label>
            <div>
              <span className="muted">Pools</span>
              <div className="checks" style={{ marginTop: 6 }}>
                {poolRows.map((p) => (
                  <label key={p.id} className="check" style={{ display: 'flex', gap: 6 }}>
                    <input type="checkbox" checked={chosen.includes(p.id)}
                           onChange={(e) => setChosen(e.target.checked
                             ? [...chosen, p.id] : chosen.filter((x) => x !== p.id))} />
                    {p.name} <span className="muted">({p.members.length} member{p.members.length === 1 ? '' : 's'})</span>
                  </label>
                ))}
              </div>
            </div>
          </div>
          {single.length > 0 && (
            <div className="banner soft">
              {single.map((p) => p.name).join(', ')} {single.length === 1 ? 'has' : 'have'} one
              member: nothing covers it while it restarts, so its network goes unpolled for the
              upgrade's duration.
            </div>
          )}
          <div style={{ marginTop: 10 }}>
            <button className="primary" disabled={!version || chosen.length === 0 || start.isPending}
                    onClick={() => start.mutate()}>
              {start.isPending ? 'Starting…' : 'Start rollout'}
            </button>
          </div>
        </fieldset>
      )}

      <fieldset className="proto">
        <legend>Rollouts</legend>
        {list.length === 0 ? <p className="muted">None yet.</p> : list.map((r) => (
          <RolloutCard key={r.id} r={r} poolName={(id) => poolRows.find((p) => p.id === id)?.name ?? id}
                       onCancel={() => cancel.mutate(r.id)} />
        ))}
      </fieldset>
    </div>
  );
}

const STATE_CLASS: Record<CollectorCommand['state'], string> = {
  pending: 'muted', delivered: 'warn', succeeded: 'ok', failed: 'critical',
  expired: 'critical', cancelled: 'muted',
};

function RolloutCard({ r, poolName, onCancel }: {
  r: Rollout; poolName: (id: string) => string; onCancel: () => void;
}) {
  return (
    <div style={{ borderTop: '1px solid var(--border)', paddingTop: 10, marginTop: 10 }}>
      <p style={{ margin: 0 }}>
        <span className="mono">{r.version}</span> →{' '}
        {r.pool_ids.map(poolName).join(', ')}{' '}
        <span className={r.state === 'succeeded' ? 'ok' : r.state === 'failed' ? 'critical'
          : r.state === 'running' ? 'warn' : 'muted'}>{r.state}</span>
        <span className="muted"> · started {relativeTime(r.created_at)}{r.created_by ? ` by ${r.created_by}` : ''}</span>
        {r.state === 'running' && <> <button onClick={onCancel}>Cancel</button></>}
      </p>
      {r.detail && <p className="muted" style={{ margin: '4px 0' }}>{r.detail}</p>}
      {r.commands.length > 0 && (
        <table>
          <thead><tr><th>Collector</th><th>State</th><th>Detail</th><th>When</th></tr></thead>
          <tbody>
            {r.commands.map((c) => (
              <tr key={c.id}>
                <td className="mono">{c.collector_id}</td>
                <td className={STATE_CLASS[c.state]}>{c.state}</td>
                <td className="muted">{c.result?.detail ?? '—'}</td>
                <td className="muted">{relativeTime(c.finished_at ?? c.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
