import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type DiscoveryBlackout, type RangeOptions } from '../../../api/client';
import { Dialog, DialogActions } from '../components/Dialog';

/** "5 Jan 14:00", in this browser's time. */
export function when(iso: string): string {
  return new Intl.DateTimeFormat(undefined, {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(iso));
}

export function useBlackouts() {
  return useQuery({ queryKey: ['discovery-blackouts'], queryFn: api.discoveryBlackouts,
                    refetchInterval: 60_000 });
}

/** Change freezes: windows in which discovery does not sweep.
 *
 *  A scheduled sweep inside one is skipped and recorded as skipped; a hand-run
 *  one is refused unless the operator overrides it. Estate-wide or one site.
 */
export function Freezes() {
  const { data } = useBlackouts();
  const [adding, setAdding] = useState(false);
  const list = data?.items ?? [];
  return (
    <section className="asset-panel disc-freezes">
      <div className="disc-rail-head">
        <h3>Change freezes</h3>
        <button type="button" className="link-button" onClick={() => setAdding(true)}>
          Add freeze
        </button>
      </div>
      {list.length === 0 ? (
        <p className="muted disc-sched-none">None.</p>
      ) : (
        <ul className="disc-sched-list">
          {list.map((b) => <FreezeItem key={b.id} b={b} />)}
        </ul>
      )}
      {adding && <FreezeDialog onClose={() => setAdding(false)} />}
    </section>
  );
}

function FreezeItem({ b }: { b: DiscoveryBlackout }) {
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ['discovery-blackouts'] });
  const end = useMutation({ mutationFn: () => api.endDiscoveryBlackout(b.id),
                            onSuccess: refresh, onError: (e) => setError(String(e)) });
  const remove = useMutation({ mutationFn: () => api.deleteDiscoveryBlackout(b.id),
                               onSuccess: refresh, onError: (e) => setError(String(e)) });
  return (
    <li className={`disc-sched disc-freeze${b.active ? ' is-active' : ''}`}>
      <div className="top">
        <span className="title">{b.name}</span>
        <span className="muted">{b.datacenter_name ?? 'all sites'}</span>
      </div>
      <div className="muted">
        {b.active ? `now until ${when(b.ends_at)}` : `${when(b.starts_at)} – ${when(b.ends_at)}`}
      </div>
      {b.reason && <div className="muted">{b.reason}</div>}
      <div className="acts">
        {b.active && (
          <button type="button" className="link-button" disabled={end.isPending}
                  onClick={() => { setError(null); end.mutate(); }}>
            End now
          </button>
        )}
        {confirming ? (
          <>
            <button type="button" className="link-button danger" disabled={remove.isPending}
                    onClick={() => { setError(null); remove.mutate(); }}>
              Confirm delete
            </button>
            <button type="button" className="link-button" onClick={() => setConfirming(false)}>
              Keep
            </button>
          </>
        ) : (
          <button type="button" className="link-button" onClick={() => setConfirming(true)}>
            Delete
          </button>
        )}
      </div>
      {error && <div className="banner">{error}</div>}
    </li>
  );
}

/** A datetime-local value for a Date, in this browser's time. */
function localInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function FreezeDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const options = useQuery<RangeOptions>({ queryKey: ['discovery-range-options'],
                                           queryFn: api.discoveryRangeOptions,
                                           staleTime: 60_000 });
  const now = new Date();
  const [name, setName] = useState('');
  const [starts, setStarts] = useState(localInput(now));
  const [ends, setEnds] = useState(localInput(new Date(now.getTime() + 24 * 3600e3)));
  const [site, setSite] = useState('');
  const [reason, setReason] = useState('');
  const [error, setError] = useState<string | null>(null);

  // datetime-local has no zone; the browser's is the one the operator typed in.
  const startIso = starts ? new Date(starts).toISOString() : '';
  const endIso = ends ? new Date(ends).toISOString() : '';
  const backwards = Boolean(startIso && endIso && endIso <= startIso);

  const save = useMutation({
    mutationFn: () => api.createDiscoveryBlackout({
      name: name.trim(), starts_at: startIso, ends_at: endIso,
      datacenter_id: site || null, reason: reason.trim() || null,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['discovery-blackouts'] });
      onClose();
    },
    onError: (e) => setError(String(e)),
  });

  return (
    <Dialog title="Add a change freeze" onClose={onClose}>
      <div className="asset-form">
        <label className="asset-form-wide">
          <span>Name</span>
          <input value={name} autoFocus onChange={(e) => setName(e.target.value)}
                 placeholder="Q4 change freeze" />
        </label>
        <label>
          <span>From</span>
          <input type="datetime-local" value={starts} onChange={(e) => setStarts(e.target.value)} />
        </label>
        <label>
          <span>Until</span>
          <input type="datetime-local" value={ends} onChange={(e) => setEnds(e.target.value)}
                 aria-invalid={backwards} />
        </label>
        <label>
          <span>Site</span>
          <select value={site} onChange={(e) => setSite(e.target.value)}>
            <option value="">All sites</option>
            {(options.data?.datacenters ?? []).map((d) => (
              <option key={d.id} value={d.id}>{d.name}</option>
            ))}
          </select>
        </label>
        <label className="asset-form-wide">
          <span>Reason</span>
          <input value={reason} onChange={(e) => setReason(e.target.value)}
                 placeholder="Change record, e.g. CHG0012345" />
        </label>
        {backwards && <p className="disc-bad asset-form-wide">Until must be after From.</p>}
        {error && <div className="banner asset-form-wide">{error}</div>}
      </div>
      <DialogActions>
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" className="primary"
                disabled={!name.trim() || !startIso || !endIso || backwards || save.isPending}
                onClick={() => { setError(null); save.mutate(); }}>
          {save.isPending ? 'Saving…' : 'Add freeze'}
        </button>
      </DialogActions>
    </Dialog>
  );
}
