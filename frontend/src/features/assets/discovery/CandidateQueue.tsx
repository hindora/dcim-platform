import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  api,
  type AssetSummary,
  type DiscoveryCandidate,
} from '../../../api/client';
import { usePaged } from '../../../components/Pagination';
import { humanise, relativeTime } from '../../../lib/format';
import { Dialog, DialogActions } from '../components/Dialog';
import { SweepPanel } from './SweepPanel';

/** The discovery queue.
 *
 *  The subsystem behind this has existed since migration 0012 - runs,
 *  candidates, an identity blob, suggested type and vendor, promote and ignore.
 *  What was missing was a screen (docs/19 B2). This is it.
 *
 *  Promote asks for the name, because discovery cannot know it - a sysDescr regex
 *  is not authority to name an inventory record. It deliberately does NOT ask for
 *  placement: a sweep cannot know which rack a box is in, and guessing would put a
 *  wrong U in the elevation. The device lands as `installed`, which is the honest
 *  reading of "something answered on the management network" - racked, and not
 *  accepted by anybody - and it then appears on the Commissioning page for
 *  somebody to accept.
 */
export function CandidateQueue() {
  const { data, isLoading, error } = useQuery<{ items: DiscoveryCandidate[] }>({
    queryKey: ['discovery-candidates'],
    queryFn: () => api.discoveryCandidates({ limit: '500' }),
    refetchInterval: 60_000,
  });

  // Serial matching is the primary key discovery is supposed to use. It cannot
  // be used while no asset carries one, and a screen that quietly falls back to
  // a weaker key than the operator assumes is worse than one that admits it.
  const { data: summary } = useQuery<AssetSummary>({
    queryKey: ['asset-summary'],
    queryFn: api.assetSummary,
  });
  const serialMatchingBlind = summary && summary.identity.with_serial === 0;

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;

  const items = data?.items ?? [];
  const unmatched = items.filter((c) => !c.matched_device_id);
  const matched = items.filter((c) => c.matched_device_id);

  return (
    <>
      <h2>Discovery</h2>

      <SweepPanel />
      {serialMatchingBlind && (
        <div className="banner">
          Serial-number matching is unavailable: no asset carries a serial.
          Candidates are matched by management IP only, so a device that has
          been re-addressed will appear as new.
        </div>
      )}

      {isLoading && <p className="muted">Loading…</p>}

      {!isLoading && items.length === 0 && (
        <div className="asset-empty">
          No discovery candidates.
        </div>
      )}

      {unmatched.length > 0 && (
        <>
          <h3>Unmatched — {unmatched.length}</h3>
          <CandidateTable rows={unmatched} />
        </>
      )}

      {matched.length > 0 && (
        <>
          <h3 style={{ marginTop: 24 }}>Already known — {matched.length}</h3>
          {/* Shown with a denominator on purpose: "the sweep saw 900 and 894
              were expected" is more useful than a list of six surprises with
              nothing to compare them against. */}
          <p className="muted">
            {matched.length} of {items.length} responders were expected.
          </p>
          <CandidateTable rows={matched} />
        </>
      )}
    </>
  );
}

function CandidateTable({ rows }: { rows: DiscoveryCandidate[] }) {
  const paged = usePaged(rows, { noun: 'candidates' });
  return (
    <>
    <div className="asset-scroll">
      <table>
        <thead>
          <tr>
            <th>Address</th><th>Protocol</th><th>Suggested</th>
            <th>Identity</th><th>Matched</th><th>Last seen</th><th>Action</th>
          </tr>
        </thead>
        <tbody>
          {paged.rows.map((c) => (
            <tr key={c.id}>
              <td className="asset-tag">{c.address ?? '—'}</td>
              <td className="muted">{c.protocol.toUpperCase()}</td>
              <td className="muted">
                {[c.suggested_vendor,
                  c.suggested_device_type ? humanise(c.suggested_device_type) : null,
                  c.suggested_model].filter(Boolean).join(' · ') || '—'}
              </td>
              <td className="muted" style={{ maxWidth: 320 }}>
                {String(c.identity?.sysDescr ?? c.identity?.sysName ?? '—')
                  .slice(0, 90)}
              </td>
              <td>
                {c.matched_device_id ? (
                  <Link to={`/assets/inventory/${c.matched_device_id}`}>
                    {c.matched_device_name ?? 'known'}
                  </Link>
                ) : (
                  <span className="asset-none">new</span>
                )}
              </td>
              <td className="muted">{relativeTime(c.last_seen)}</td>
              <td><CandidateActions c={c} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
    {paged.foot}
    </>
  );
}


/** Promote or dismiss one responder.
 *
 *  Nothing is offered for a candidate that already matches a device in inventory:
 *  promoting it would create a duplicate, and the API refuses for that reason. The
 *  honest action there is to open the device it matched.
 */
function CandidateActions({ c }: { c: DiscoveryCandidate }) {
  const qc = useQueryClient();
  const [naming, setNaming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['discovery-candidates'] });
    qc.invalidateQueries({ queryKey: ['asset-summary'] });
    // A promoted device arrives as `installed`, so it belongs to that queue now.
    qc.invalidateQueries({ queryKey: ['commissioning-queue'] });
  };

  const dismiss = useMutation({
    mutationFn: () => api.ignoreCandidate(c.id),
    onSuccess: refresh,
    onError: (e) => setError(String(e)),
  });

  if (c.matched_device_id) {
    return (
      <Link to={`/assets/inventory/${c.matched_device_id}`}>Open</Link>
    );
  }
  if (c.status !== 'new') {
    return <span className="muted">{humanise(c.status)}</span>;
  }

  return (
    <>
      <button type="button" onClick={() => setNaming(true)}>Promote</button>
      <button type="button" disabled={dismiss.isPending}
              onClick={() => { setError(null); dismiss.mutate(); }}>
        Ignore
      </button>
      {error && <div className="banner">{error}</div>}
      {naming && (
        <PromoteDialog c={c} onClose={() => setNaming(false)} onDone={refresh} />
      )}
    </>
  );
}

function PromoteDialog({ c, onClose, onDone }: {
  c: DiscoveryCandidate; onClose: () => void; onDone: () => void;
}) {
  const [name, setName] = useState(
    String(c.identity?.sysName ?? '').trim());
  const [type, setType] = useState(c.suggested_device_type ?? '');
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () => api.promoteCandidate(c.id, {
      name: name.trim(),
      device_type: type || undefined,
    }),
    onSuccess: () => { onDone(); onClose(); },
    onError: (e) => setError(String(e)),
  });

  return (
    <Dialog title={`Promote ${c.address ?? 'responder'}`} onClose={onClose}>
      <div className="asset-form">
        {/* Pre-filled from sysName because that is usually what the device calls
            itself, and left editable because a device naming itself is not the
            same as the estate's naming scheme. */}
        <label>
          <span>Name</span>
          <input value={name} autoFocus
                 onChange={(e) => setName(e.target.value)}
                 placeholder="as the estate names it" />
        </label>
        <label>
          <span>Device type</span>
          <input value={type} onChange={(e) => setType(e.target.value)}
                 placeholder={c.suggested_device_type ?? 'switch'} />
        </label>
        <p className="muted asset-form-wide">
          It will be recorded as <strong>installed</strong> — racked, and not
          accepted into service. Placement is not asked for: a sweep cannot know
          which rack it is in. Accept it from the Commissioning page once you are
          satisfied with it.
        </p>
        {error && <div className="banner">{error}</div>}
      </div>
      <DialogActions>
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" disabled={!name.trim() || save.isPending}
                onClick={() => { setError(null); save.mutate(); }}>
          {save.isPending ? 'Promoting…' : 'Promote'}
        </button>
      </DialogActions>
    </Dialog>
  );
}
