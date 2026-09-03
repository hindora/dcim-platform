import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError, type LifecycleHistory } from '../../../api/client';
import { humanise, relativeTime } from '../../../lib/format';
import { LifecycleChip } from '../components/LifecycleChip';

/** An asset's lifecycle: where it is, where it may go, and how it got here.
 *
 *  The allowed transitions come from the SERVER. Hard-coding them here would
 *  give the UI a second copy of the matrix, and the two would drift - the
 *  browser offering a move the API refuses is worse than not offering it,
 *  because the operator has already decided by the time they are told no.
 */
export function LifecycleTab({ deviceId }: { deviceId: string }) {
  const qc = useQueryClient();
  const [target, setTarget] = useState('');
  const [reason, setReason] = useState('');
  const [changeRef, setChangeRef] = useState('');
  const [error, setError] = useState<string | null>(null);

  const { data, isLoading } = useQuery<LifecycleHistory>({
    queryKey: ['lifecycle', deviceId],
    queryFn: () => api.lifecycleHistory(deviceId),
  });

  const move = useMutation({
    mutationFn: () => api.lifecycleTransition(deviceId, {
      to_state: target,
      reason: reason || undefined,
      change_ref: changeRef || undefined,
    }),
    onSuccess: () => {
      setTarget(''); setReason(''); setChangeRef(''); setError(null);
      qc.invalidateQueries({ queryKey: ['lifecycle', deviceId] });
      qc.invalidateQueries({ queryKey: ['device', deviceId] });
      qc.invalidateQueries({ queryKey: ['asset-summary'] });
    },
    onError: (e) => {
      // The API refuses with the allowed set attached. Showing its message is
      // the point - "in_service cannot go to in_stock; allowed: maintenance,
      // decommissioned" tells the operator what to do instead.
      setError(e instanceof ApiError ? e.message : String(e));
    },
  });

  if (isLoading || !data) return <p className="muted">Loading…</p>;

  return (
    <>
      <div className="asset-panel" style={{ marginBottom: 18 }}>
        <h3>Move this asset</h3>
        <div className="asset-move">
          <div>
            <label htmlFor="lc-to">New state</label>
            <select id="lc-to" value={target}
                    onChange={(e) => { setTarget(e.target.value); setError(null); }}>
              <option value="">Choose…</option>
              {data.allowed.map((s) => (
                <option key={s} value={s}>{humanise(s)}</option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="lc-reason">Reason</label>
            <input id="lc-reason" type="text" value={reason}
                   placeholder="What is happening, and why"
                   onChange={(e) => setReason(e.target.value)} />
          </div>
          <div>
            <label htmlFor="lc-ref">Change reference</label>
            <input id="lc-ref" type="text" value={changeRef}
                   placeholder="CHG-…"
                   onChange={(e) => setChangeRef(e.target.value)} />
          </div>
          <button type="button" disabled={!target || move.isPending}
                  onClick={() => move.mutate()}>
            {move.isPending ? 'Recording…' : 'Record transition'}
          </button>
        </div>

        {data.allowed.length === 0 && (
          <p className="muted" style={{ marginTop: 8 }}>
            {humanise(data.current ?? '')} is terminal — this asset has left the
            estate and cannot move again.
          </p>
        )}
        {error && <div className="banner" style={{ marginTop: 10 }}>{error}</div>}
      </div>

      <h3>History</h3>
      {data.events.length === 0 ? (
        <div className="asset-empty">No recorded transitions.</div>
      ) : (
        <div className="asset-scroll">
          <table>
            <thead>
              <tr>
                <th>When</th><th>From</th><th>To</th>
                <th>Reason</th><th>Change</th><th>By</th>
              </tr>
            </thead>
            <tbody>
              {data.events.map((e) => (
                <tr key={e.id}>
                  <td className="muted" title={e.ts}>{relativeTime(e.ts)}</td>
                  <td>
                    {e.from_state
                      ? <LifecycleChip state={e.from_state} />
                      : <span className="asset-none">—</span>}
                  </td>
                  <td><LifecycleChip state={e.to_state} /></td>
                  <td>{e.reason ?? <span className="asset-none">—</span>}</td>
                  <td className="asset-tag">
                    {e.change_ref ?? <span className="asset-none">—</span>}
                  </td>
                  <td className="muted">{e.actor}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
