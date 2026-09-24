import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  api,
  type DiscoveryRun,
} from '../../../api/client';
import { relativeTime } from '../../../lib/format';

/** Run a sweep, and see what the last ones did.
 *
 *  This is how the DCIM learns that hardware exists: it asks the management
 *  network, over the protocols the devices actually speak, from the collector
 *  that sits on that network. It talks to no other product's API - a DCIM must not
 *  know what is generating its telemetry.
 *
 *  Queued, not executed here. The API records the run and a collector claims it,
 *  because the sweep happens on the management network and the API is not on it.
 */
export function SweepPanel() {
  const qc = useQueryClient();
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [extra, setExtra] = useState('');
  const [error, setError] = useState<string | null>(null);

  const { data: subnets } = useQuery({
    queryKey: ['discovery-subnets'],
    queryFn: api.discoverySubnets,
  });

  const { data: runs } = useQuery({
    queryKey: ['discovery-runs'],
    queryFn: () => api.discoveryRuns({ limit: '5' }),
    // Only while something is in flight. A sweep is a background audit, not an
    // emergency, so polling it every few seconds all day would cost more than it
    // tells anybody.
    refetchInterval: (q) =>
      q.state.data?.items?.some((r) => r.status === 'pending'
        || r.status === 'running') ? 5_000 : false,
  });

  const chosen = [
    ...picked,
    ...extra.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean),
  ];

  const start = useMutation({
    mutationFn: () => api.startDiscoveryRun({ subnets: chosen }),
    onSuccess: () => {
      setError(null);
      qc.invalidateQueries({ queryKey: ['discovery-runs'] });
      qc.invalidateQueries({ queryKey: ['discovery-candidates'] });
    },
    // The API refuses a /16 rather than truncating it - sweeping the first 4096
    // of 65,536 and reporting "found 12" would be a lie about what was audited.
    // Its message says so; ours would not.
    onError: (e) => setError(String(e)),
  });

  const toggle = (cidr: string) => setPicked((s) => {
    const next = new Set(s);
    if (next.has(cidr)) next.delete(cidr); else next.add(cidr);
    return next;
  });

  const inFlight = runs?.items?.find(
    (r) => r.status === 'pending' || r.status === 'running');

  return (
    <section className="asset-panel">
      <h3>Sweep the management network</h3>
      <p className="muted">
        Asks every address in the chosen subnets whether anything answers, and
        stages what does as a candidate below. Bounded and deliberately slow — a
        sweep is a background audit, and an unbounded one looks like a port scan
        to anything watching.
      </p>

      {/* Derived from inventory rather than typed from memory. The count is what
          makes a result readable: 97 responders in a subnet with 96 known is one
          thing worth looking at. */}
      {subnets?.subnets?.length ? (
        <div className="asset-form-wide" style={{ marginBottom: 8 }}>
          {subnets.subnets.map((s) => (
            <label key={s.cidr} className="asset-check">
              <input type="checkbox" checked={picked.has(s.cidr)}
                     onChange={() => toggle(s.cidr)} />
              <code>{s.cidr}</code>
              <span className="muted"> — {s.known} known</span>
            </label>
          ))}
        </div>
      ) : (
        <p className="muted">
          No management addresses in inventory yet, so there is nothing to suggest.
          Enter a subnet below.
        </p>
      )}

      <label className="asset-form-wide">
        <span>Other subnets</span>
        <input value={extra} onChange={(e) => setExtra(e.target.value)}
               placeholder="10.51.30.0/24, 10.52.30.0/24" />
      </label>

      <div className="asset-toolbar">
        <button type="button"
                disabled={chosen.length === 0 || start.isPending
                          || Boolean(inFlight)}
                onClick={() => start.mutate()}>
          {inFlight ? 'Sweep in progress…'
            : start.isPending ? 'Queueing…'
              : `Run sweep${chosen.length ? ` (${chosen.length})` : ''}`}
        </button>
        {chosen.length === 0 && (
          <span className="muted">Choose at least one subnet.</span>
        )}
      </div>

      {error && <div className="banner">{error}</div>}

      {runs?.items?.length ? (
        <>
          <h4 style={{ marginTop: 16 }}>Recent sweeps</h4>
          <div className="asset-scroll">
            <table>
              <thead>
                <tr>
                  <th>Started</th><th>Subnets</th><th>Status</th>
                  <th>Answered</th><th>Promoted</th>
                </tr>
              </thead>
              <tbody>
                {runs.items.map((r) => <RunRow key={r.id} run={r} />)}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </section>
  );
}

function RunRow({ run }: { run: DiscoveryRun }) {
  return (
    <tr>
      <td className="muted">{relativeTime(run.started_at)}</td>
      <td><code>{(run.scope?.subnets ?? []).join(', ') || '—'}</code></td>
      <td>
        <span className={`asset-life is-${run.status}`}>{run.status}</span>
        {run.error && <div className="muted">{run.error}</div>}
      </td>
      <td>{run.found ?? '—'}</td>
      <td className="muted">{run.promoted ?? '—'}</td>
    </tr>
  );
}
