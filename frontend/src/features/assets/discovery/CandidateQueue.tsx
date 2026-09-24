import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  api,
  type AssetFilterOptions,
  type DiscoveryCandidate,
} from '../../../api/client';
import { usePaged } from '../../../components/Pagination';
import { humanise, relativeTime } from '../../../lib/format';
import { Dialog, DialogActions } from '../components/Dialog';
import { SweepPanel } from './SweepPanel';

/** Discovery: what answered on the management network, and what to do about it.
 *
 *  The value of an audit is the EXCEPTION, so the page is ordered by how much a
 *  row wants attention rather than by what the sweep happened to return. A
 *  hundred expected devices collapse to one line; the one that moved does not.
 *
 *  Promote asks for the name, because discovery cannot know it - a sysDescr regex
 *  is not authority to name an inventory record. It deliberately does NOT ask for
 *  placement: a sweep cannot know which rack a box is in, and guessing would put a
 *  wrong U in the elevation. The device lands as `installed` - racked, and not
 *  accepted by anybody - and then appears on the Commissioning page.
 */
export function CandidateQueue() {
  const { data, isLoading, error } = useQuery<{ items: DiscoveryCandidate[] }>({
    queryKey: ['discovery-candidates'],
    queryFn: () => api.discoveryCandidates({ limit: '500' }),
    refetchInterval: 60_000,
  });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;

  const items = data?.items ?? [];
  const unmatched = items.filter((c) => !c.matched_device_id);
  // Matched, but not where inventory says it is. Only knowable because the serial
  // matched - on address alone this row would have read as something new, and
  // promoting it would have created a second record for one physical box.
  const moved = items.filter((c) => c.matched_device_id
    && c.matched_device_address && c.matched_device_address !== c.address);
  const expected = items.filter((c) => c.matched_device_id && !moved.includes(c));

  // How much of this result can be trusted. A responder with no serial is matched
  // by address alone, so a device that has been re-addressed still reads as new.
  // Saying which proportion that is beats a banner that guesses.
  const withSerial = items.filter((c) => c.serial).length;

  return (
    <>
      <h2>Discovery</h2>

      <SweepPanel />

      {isLoading && <p className="muted">Loading…</p>}

      {!isLoading && items.length === 0 && (
        <div className="asset-empty">
          Nothing has answered yet. Run a sweep above: it asks every address in a
          subnet whether something is there, and stages what replies here for you
          to promote into inventory or dismiss.
        </div>
      )}

      {items.length > 0 && (
        <p className="muted">
          {withSerial} of {items.length} responders reported a serial.
          {withSerial < items.length && (
            <> The other {items.length - withSerial} are matched by address
            alone, so one that has been re-addressed will read as new.</>
          )}
        </p>
      )}

      {moved.length > 0 && (
        <section style={{ marginTop: 16 }}>
          <h3>Moved — {moved.length}</h3>
          <p className="muted">
            Recognised by serial at an address inventory does not expect. The
            hardware is where it says; the record is stale.
          </p>
          <CandidateTable rows={moved} />
        </section>
      )}

      {unmatched.length > 0 && (
        <section style={{ marginTop: 16 }}>
          <h3>Not in inventory — {unmatched.length}</h3>
          <p className="muted">
            Something is answering that no record accounts for: installed and
            never recorded, re-addressed with no serial to match on, or not
            supposed to be there at all.
          </p>
          <CandidateTable rows={unmatched} />
        </section>
      )}

      {expected.length > 0 && (
        <section style={{ marginTop: 16 }}>
          {/* Collapsed by default. The expected case getting the same visual
              weight as the surprise is what turns an audit into a data dump -
              but the denominator matters, so the count stays visible. */}
          <Expected rows={expected} total={items.length} />
        </section>
      )}
    </>
  );
}

function Expected({ rows, total }: { rows: DiscoveryCandidate[]; total: number }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <h3>
        <button type="button" className="asset-link"
                aria-expanded={open}
                onClick={() => setOpen((o) => !o)}>
          {open ? '▾' : '▸'} Where we expected them — {rows.length} of {total}
        </button>
      </h3>
      {open && <CandidateTable rows={rows} />}
    </>
  );
}

function CandidateTable({ rows }: { rows: DiscoveryCandidate[] }) {
  const paged = usePaged(rows, { noun: 'responders' });
  return (
    <>
      <div className="asset-scroll">
        <table>
          <thead>
            <tr>
              <th>Address</th><th>Identity</th><th>Serial</th>
              <th>Matched</th><th>Suggested</th><th>Last seen</th><th>Action</th>
            </tr>
          </thead>
          <tbody>
            {paged.rows.map((c) => <Row key={c.id} c={c} />)}
          </tbody>
        </table>
      </div>
      {paged.foot}
    </>
  );
}

function Row({ c }: { c: DiscoveryCandidate }) {
  const descr = String(c.identity?.sysDescr ?? c.identity?.sysName ?? '');
  const movedFrom = c.matched_device_address && c.matched_device_address !== c.address
    ? c.matched_device_address : null;

  return (
    <tr>
      <td className="asset-tag">
        {c.address ?? '—'}
        <div className="muted">{c.protocol.toUpperCase()}</div>
      </td>
      {/* sysDescr is where the model and the firmware live, and it is the whole
          evidence for the suggested type - so the full string is available on
          hover rather than cut off at 90 characters with no way to see it. */}
      <td className="muted" style={{ maxWidth: 320 }} title={descr || undefined}>
        {descr ? `${descr.slice(0, 90)}${descr.length > 90 ? '…' : ''}` : '—'}
      </td>
      <td className="asset-tag">
        {c.serial ?? <span className="asset-none">not reported</span>}
      </td>
      <td>
        {c.matched_device_id ? (
          <>
            <Link to={`/assets/inventory/${c.matched_device_id}`}>
              {c.matched_device_name ?? 'known'}
            </Link>
            <div className="muted">
              {c.matched_on_serial ? 'by serial' : 'by address'}
              {movedFrom && <> · recorded at {movedFrom}</>}
            </div>
          </>
        ) : (
          <span className="asset-none">new</span>
        )}
      </td>
      <td className="muted">
        {[c.suggested_vendor,
          c.suggested_device_type ? humanise(c.suggested_device_type) : null,
          c.suggested_model].filter(Boolean).join(' · ') || '—'}
      </td>
      <td className="muted">{relativeTime(c.last_seen)}</td>
      <td><CandidateActions c={c} /></td>
    </tr>
  );
}

/** Promote or dismiss one responder.
 *
 *  Nothing is offered for a candidate that already matches a device: promoting it
 *  would create a duplicate, and the API refuses for that reason. The honest
 *  action there is to open the device it matched — and for a MOVED one, to correct
 *  its address on the record.
 */
function CandidateActions({ c }: { c: DiscoveryCandidate }) {
  const qc = useQueryClient();
  const [naming, setNaming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['discovery-candidates'] });
    qc.invalidateQueries({ queryKey: ['discovery-runs'] });
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
    return <Link to={`/assets/inventory/${c.matched_device_id}`}>Open</Link>;
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
  const [name, setName] = useState(String(c.identity?.sysName ?? '').trim());
  const [type, setType] = useState(c.suggested_device_type ?? '');
  const [error, setError] = useState<string | null>(null);

  // The real vocabulary, not a free-text box. A typo here creates a device with a
  // garbage type that every category roll-up then has to cope with.
  const { data: options } = useQuery<AssetFilterOptions>({
    queryKey: ['asset-filter-options'],
    queryFn: api.assetFilterOptions,
  });

  const save = useMutation({
    mutationFn: () => api.promoteCandidate(c.id, {
      name: name.trim(), device_type: type || undefined,
    }),
    onSuccess: () => { onDone(); onClose(); },
    onError: (e) => setError(String(e)),
  });

  return (
    <Dialog title={`Promote ${c.address ?? 'responder'}`} onClose={onClose}>
      <div className="asset-form">
        {/* Pre-filled from sysName because that is usually what the device calls
            itself, and editable because a device naming itself is not the same as
            the estate's naming scheme. */}
        <label>
          <span>Name</span>
          <input value={name} autoFocus
                 onChange={(e) => setName(e.target.value)}
                 placeholder="as the estate names it" />
        </label>
        <label>
          <span>Device type</span>
          <select value={type} onChange={(e) => setType(e.target.value)}>
            <option value="">— choose —</option>
            {(options?.device_types ?? []).map((t) => (
              <option key={t.code} value={t.code}>
                {t.display_name}{t.code === c.suggested_device_type
                  ? ' (suggested)' : ''}
              </option>
            ))}
          </select>
        </label>

        {/* Shown, not silently applied. The sweep's guess is evidence for the
            operator to accept, and promote records only the name and the type -
            vendor and model need a vendor lookup this endpoint does not do. */}
        {(c.suggested_vendor || c.suggested_model || c.serial) && (
          <p className="muted asset-form-wide">
            The sweep read:{' '}
            {[c.suggested_vendor, c.suggested_model,
              c.serial ? `serial ${c.serial}` : null]
              .filter(Boolean).join(' · ')}.
            {(c.suggested_vendor || c.suggested_model) && (
              <> Vendor and model are not recorded by promotion — set them on the
              asset afterwards.</>
            )}
          </p>
        )}

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
        <button type="button" disabled={!name.trim() || !type || save.isPending}
                onClick={() => { setError(null); save.mutate(); }}>
          {save.isPending ? 'Promoting…' : 'Promote'}
        </button>
      </DialogActions>
    </Dialog>
  );
}
