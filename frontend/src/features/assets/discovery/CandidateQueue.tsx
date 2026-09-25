import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  api,
  type AssetFilterOptions,
  type AttachableDevice,
  type DiscoveryCandidate,
} from '../../../api/client';
import { usePaged } from '../../../components/Pagination';
import {
  collapse, isGone, isKnown, matchOf, memberIds, movedFrom,
  type Responder,
} from './responders';
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

  // Fetched separately because the default list is open candidates: a dismissed
  // responder is deliberately out of the way, and mixing it back into the main
  // groups would undo the dismissal.
  //
  // Declared BEFORE the error return. A hook after an early return is called
  // conditionally, so the first failed fetch would change the hook count between
  // renders and React would throw - hiding the real error behind a crash.
  const { data: dismissed } = useQuery<{ items: DiscoveryCandidate[] }>({
    queryKey: ['discovery-candidates', 'ignored'],
    queryFn: () => api.discoveryCandidates({ status: 'ignored', limit: '200' }),
    refetchInterval: 60_000,
  });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;

  // One row per MACHINE, not per probe. A server answering SNMP on its BMC and
  // Redfish on the same address was two rows, and only the Redfish one carried a
  // serial - so the page showed a blank Serial column beside a populated one for
  // the same box and read as a defect.
  const all = collapse(data?.items ?? []);
  const ignored = collapse(dismissed?.items ?? []);
  // Responders that answered nothing on the last sweep of their address. Out of the
  // three buckets below, because "something is answering that no record accounts
  // for" is a claim about the present tense, and a machine that has gone quiet makes
  // that sentence false - which is the one thing an audit page must not do.
  const gone = all.filter(isGone);
  const items = all.filter((r) => !isGone(r));
  const unmatched = items.filter((r) => !isKnown(r));
  // Matched, but not where inventory says it is. Only knowable because the serial
  // matched - on address alone this row would have read as something new, and
  // promoting it would have created a second record for one physical box.
  const moved = items.filter((r) => isKnown(r) && movedFrom(r));
  const expected = items.filter((r) => isKnown(r) && !movedFrom(r));

  // How much of this result can be trusted. A responder with no serial is matched
  // by address alone, so a device that has been re-addressed still reads as new.
  // Saying which proportion that is beats a banner that guesses.
  //
  // Counted over machines now that a machine is one row, which is also the honest
  // denominator: a server whose Redfish probe read a serial IS identified, and
  // counting its silent SNMP probe against it understated the coverage.
  const withSerial = items.filter((r) => r.serial).length;

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
          <CandidateTable rows={unmatched} bulk />
        </section>
      )}

      {ignored.length > 0 && (
        <section style={{ marginTop: 16 }}>
          {/* "What have we decided not to look at" is a question worth being able
              to answer: a responder somebody waved away is exactly where an
              unmanaged box hides. Ignore used to be one-way and invisible. */}
          <Dismissed rows={ignored} />
        </section>
      )}

      {gone.length > 0 && (
        <section style={{ marginTop: 16 }}>
          <Gone rows={gone} />
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

function Dismissed({ rows }: { rows: Responder[] }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <h3>
        <button type="button" className="asset-link" aria-expanded={open}
                onClick={() => setOpen((o) => !o)}>
          {open ? '▾' : '▸'} Dismissed — {rows.length}
        </button>
      </h3>
      {open && (
        <>
          <p className="muted">
            Responders somebody decided were not worth recording. Restoring one
            puts it back in the queue above.
          </p>
          <CandidateTable rows={rows} />
        </>
      )}
    </>
  );
}

/** Answered once, and not on the last sweep that covered the address.
 *
 *  Collapsed, because it is history rather than work. Kept rather than deleted: that
 *  a machine used to answer at an address is how somebody later works out what used
 *  to be there, and for a device removed without being decommissioned it is the only
 *  trace there is.
 */
function Gone({ rows }: { rows: Responder[] }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <h3>
        <button type="button" className="asset-link" aria-expanded={open}
                onClick={() => setOpen((o) => !o)}>
          {open ? '▾' : '▸'} Stopped answering — {rows.length}
        </button>
      </h3>
      {open && (
        <>
          <p className="muted">
            These answered a previous sweep and did not answer the most recent one
            that covered their address. A sweep of a different subnet does not put a
            responder here: the run records what it swept, so this is the difference
            between asked-and-silent and never-asked.
          </p>
          <CandidateTable rows={rows} />
        </>
      )}
    </>
  );
}

function Expected({ rows, total }: { rows: Responder[]; total: number }) {
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

function CandidateTable({ rows, bulk = false }: {
  rows: Responder[]; bulk?: boolean;
}) {
  const paged = usePaged(rows, { noun: 'responders' });
  // Keyed by responder, not by candidate: a selection has to mean "this machine",
  // or ignoring a row would dismiss one of its protocols and leave the other.
  const [picked, setPicked] = useState<Set<string>>(new Set());

  // Only what is on the page, and only what is still actionable. A "select all"
  // that quietly included rows the operator cannot see is how a bulk action
  // surprises somebody.
  const selectable = paged.rows.filter((r) => r.primary.status === 'new');
  const allPicked = selectable.length > 0
    && selectable.every((r) => picked.has(r.key));

  const toggle = (key: string) => setPicked((p) => {
    const next = new Set(p);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  return (
    <>
      {bulk && (
        <BulkBar keys={[...picked]} rows={rows}
                 onDone={() => setPicked(new Set())} />
      )}
      <div className="asset-scroll">
        <table>
          <thead>
            <tr>
              {bulk && (
                <th>
                  <input type="checkbox" checked={allPicked}
                         aria-label="Select every responder on this page"
                         onChange={() => setPicked((p) => {
                           const next = new Set(p);
                           if (allPicked) selectable.forEach((r) => next.delete(r.key));
                           else selectable.forEach((r) => next.add(r.key));
                           return next;
                         })} />
                </th>
              )}
              <th>Address</th><th>Identity</th><th>Serial</th>
              <th>Matched</th><th>Suggested</th><th>Last seen</th><th>Action</th>
            </tr>
          </thead>
          <tbody>
            {paged.rows.map((r) => (
              <Row key={r.key} r={r}
                   picked={bulk ? picked.has(r.key) : undefined}
                   onPick={bulk ? () => toggle(r.key) : undefined} />
            ))}
          </tbody>
        </table>
      </div>
      {paged.foot}
    </>
  );
}

/** Act on several responders at once.
 *
 *  Promote takes NO name field. A bulk promote cannot ask for forty names, and
 *  deriving them from addresses would put IP addresses into the estate's naming
 *  scheme permanently - so this only promotes responders that already say what they
 *  are called, and the count says how many of the selection that is. The rest are
 *  named one at a time, which is the honest amount of work.
 */
function BulkBar({ keys, rows, onDone }: {
  keys: string[]; rows: Responder[]; onDone: () => void;
}) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<string | null>(null);

  const chosen = rows.filter((r) => keys.includes(r.key));
  // A name off ANY of the machine's probes: a BMC reports `hostName` over Redfish
  // where its SNMP agent reports nothing, and refusing to promote for want of a
  // name the machine did give us would be the same mistake the blank Serial column
  // was.
  const named = (r: Responder) => r.members
    .map((c) => String(c.identity?.hostName ?? c.identity?.sysName ?? '').trim())
    .find(Boolean);
  const nameable = chosen.filter(named);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['discovery-candidates'] });
    qc.invalidateQueries({ queryKey: ['discovery-runs'] });
    qc.invalidateQueries({ queryKey: ['asset-summary'] });
    qc.invalidateQueries({ queryKey: ['commissioning-queue'] });
    onDone();
  };

  const promote = useMutation({
    // The primary of each machine - the probe that read a serial, so the device
    // lands with the one key that survives it being re-addressed.
    mutationFn: () => api.bulkPromoteCandidates(nameable.map((r) => r.primary.id)),
    onSuccess: (r) => {
      setError(null);
      // Skipped rows are reported rather than silently dropped: an operator who
      // selected forty and promoted thirty-seven needs to know which three, and
      // why.
      setReport(`${r.promoted.length} promoted`
        + (r.skipped.length ? `, ${r.skipped.length} skipped (no name reported)` : '')
        + (r.failed.length ? `, ${r.failed.length} failed` : ''));
      refresh();
    },
    onError: (e) => setError(String(e)),
  });

  // EVERY probe on every selected machine. Dismissing the SNMP half and leaving
  // the Redfish half would keep the row in the queue asking about a box somebody
  // has already waved away.
  const dismiss = useMutation({
    mutationFn: () => api.bulkIgnoreCandidates(chosen.flatMap(memberIds)),
    onSuccess: (r) => {
      setError(null);
      setReport(`${chosen.length} dismissed`
        + (r.ignored !== chosen.length ? ` (${r.ignored} probes)` : '')
        + (r.failed.length ? `, ${r.failed.length} failed` : ''));
      refresh();
    },
    onError: (e) => setError(String(e)),
  });

  if (keys.length === 0) {
    return report ? <p className="muted">{report}</p> : null;
  }

  return (
    <div className="asset-toolbar">
      <span className="muted">{keys.length} selected</span>
      <button type="button"
              disabled={nameable.length === 0 || promote.isPending}
              onClick={() => { setError(null); promote.mutate(); }}>
        {promote.isPending ? 'Promoting…'
          : `Promote ${nameable.length} by reported name`}
      </button>
      <button type="button" disabled={dismiss.isPending}
              onClick={() => { setError(null); dismiss.mutate(); }}>
        {dismiss.isPending ? 'Dismissing…' : `Ignore ${keys.length}`}
      </button>
      {nameable.length < keys.length && (
        <span className="muted">
          {keys.length - nameable.length} of these report no name and must be
          promoted individually.
        </span>
      )}
      {error && <div className="banner">{error}</div>}
      {report && <span className="muted">{report}</span>}
    </div>
  );
}

function Row({ r, picked, onPick }: {
  r: Responder; picked?: boolean; onPick?: () => void;
}) {
  const c = r.primary;
  // The fullest description any probe returned. A Redfish service root carries a
  // version and a name and no model at all, so taking the primary's would often
  // throw away the sysDescr that is the whole evidence for the suggested type.
  const descr = r.members
    .map((m) => String(m.identity?.sysDescr ?? m.identity?.sysName ?? ''))
    .reduce((a, b) => (b.length > a.length ? b : a), '');
  const match = matchOf(r);
  const from = movedFrom(r);
  // Newest reading across the machine's probes: a row saying "2 min ago" because
  // one protocol answered is more use than one saying "an hour ago" because the
  // other did not.
  const lastSeen = r.members
    .map((m) => m.last_seen)
    .reduce((a, b) => (String(b ?? '') > String(a ?? '') ? b : a), r.primary.last_seen);
  // Suggestions from whichever probe had one. Redfish suggests `server` where a
  // bare sysDescr suggests nothing, and the vendor often comes from the other one.
  const pick = <K extends keyof DiscoveryCandidate>(k: K) =>
    r.members.map((m) => m[k]).find(Boolean);

  return (
    <tr>
      {onPick && (
        <td>
          {/* Only an actionable row is selectable: a promoted one has nothing left
              to do to it, and including it would make a count that lies. */}
          {c.status === 'new' && (
            <input type="checkbox" checked={Boolean(picked)} onChange={onPick}
                   aria-label={`Select ${c.address ?? 'responder'}`} />
          )}
        </td>
      )}
      <td className="asset-tag">
        {r.addresses.length > 0
          ? r.addresses.map((a) => <div key={a}>{a}</div>)
          : '—'}
        {/* Which protocols reached it. Two badges on one row is the answer to why
            the Serial column can be populated for a machine whose SNMP agent
            publishes no serial OID. */}
        <div className="muted">{r.protocols.join(' · ')}</div>
      </td>
      <td className="muted" style={{ maxWidth: 320 }}><Identity descr={descr} /></td>
      <td className="asset-tag">
        {r.serial ?? <span className="asset-none">not reported</span>}
        {/* Attributed, because "which protocol told us" is the useful half: it says
            a blank here is a property of the protocol, not a missing serial. */}
        {r.serial && r.protocols.length > 1 && (
          <div className="muted">via {r.serialFrom}</div>
        )}
      </td>
      <td>
        {match?.matched_device_id ? (
          <>
            <Link to={`/assets/inventory/${match.matched_device_id}`}>
              {match.matched_device_name ?? 'known'}
            </Link>
            <div className="muted">
              {match.matched_on_serial ? 'by serial' : 'by address'}
              {from && <> · recorded at {from}</>}
            </div>
          </>
        ) : (
          <span className="asset-none">new</span>
        )}
      </td>
      <td className="muted">
        {[pick('suggested_vendor'),
          pick('suggested_device_type')
            ? humanise(String(pick('suggested_device_type'))) : null,
          pick('suggested_model')].filter(Boolean).join(' · ') || '—'}
      </td>
      <td className="muted">{relativeTime(lastSeen)}</td>
      <td><CandidateActions r={r} /></td>
    </tr>
  );
}

/** sysDescr, expandable.
 *
 *  It is where the model and the firmware live, and it is the whole evidence for
 *  the suggested type - so cutting it at 90 characters with no way to see the rest
 *  hides the reason the row says "switch". This was a `title` tooltip, which is
 *  unreachable by keyboard and invisible on a touch screen; a button is neither.
 */
function Identity({ descr }: { descr: string }) {
  const [open, setOpen] = useState(false);
  if (!descr) return <>—</>;
  if (descr.length <= 90) return <>{descr}</>;
  return (
    <>
      {open ? descr : `${descr.slice(0, 90)}…`}{' '}
      <button type="button" className="asset-link" aria-expanded={open}
              onClick={() => setOpen((o) => !o)}>
        {open ? 'less' : 'more'}
      </button>
    </>
  );
}

/** Promote or dismiss one responder.
 *
 *  Nothing is offered for a candidate that already matches a device: promoting it
 *  would create a duplicate, and the API refuses for that reason. The honest
 *  action there is to open the device it matched — and for a MOVED one, to correct
 *  its address on the record.
 */
function CandidateActions({ r }: { r: Responder }) {
  const c = r.primary;
  const qc = useQueryClient();
  const [naming, setNaming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    // Keyed prefix, so both the open list and the dismissed one refetch - a
    // restored responder has to leave one and appear in the other.
    qc.invalidateQueries({ queryKey: ['discovery-candidates'] });
    qc.invalidateQueries({ queryKey: ['discovery-runs'] });
    qc.invalidateQueries({ queryKey: ['asset-summary'] });
    // A promoted device arrives as `installed`, so it belongs to that queue now.
    qc.invalidateQueries({ queryKey: ['commissioning-queue'] });
  };

  // Every probe on the machine, not just the one this row happens to be built
  // around. Dismissing SNMP and leaving Redfish keeps the row in the queue.
  const ids = memberIds(r);
  const dismiss = useMutation({
    mutationFn: () => (ids.length > 1
      ? api.bulkIgnoreCandidates(ids) : api.ignoreCandidate(c.id)),
    onSuccess: refresh,
    onError: (e) => setError(String(e)),
  });

  const restore = useMutation({
    mutationFn: () => (ids.length > 1
      ? Promise.all(ids.map((id) => api.unignoreCandidate(id)))
      : api.unignoreCandidate(c.id)),
    onSuccess: refresh,
    onError: (e) => setError(String(e)),
  });

  const match = matchOf(r);
  if (match?.matched_device_id) {
    return <Link to={`/assets/inventory/${match.matched_device_id}`}>Open</Link>;
  }
  if (c.status === 'ignored') {
    return (
      <>
        <button type="button" disabled={restore.isPending}
                onClick={() => { setError(null); restore.mutate(); }}>
          {restore.isPending ? 'Restoring…' : 'Un-ignore'}
        </button>
        {error && <div className="banner">{error}</div>}
      </>
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
  const reported = String(c.identity?.hostName ?? c.identity?.sysName ?? '').trim();
  const [name, setName] = useState(reported);
  const [type, setType] = useState(c.suggested_device_type ?? '');
  const [attachTo, setAttachTo] = useState('');
  const [error, setError] = useState<string | null>(null);

  // Records somebody is waiting for. Offered because promotion used to INSERT
  // unconditionally, which left two rows for one machine: the placeholder still
  // holding its rack unit, and a discovered device with no placement at all.
  const { data: attachable } = useQuery<{ items: AttachableDevice[] }>({
    queryKey: ['attachable', type],
    queryFn: () => api.attachableDevices(type || undefined),
  });
  const targets = attachable?.items ?? [];
  const target = targets.find((t) => t.id === attachTo);

  // The real vocabulary, not a free-text box. A typo here creates a device with a
  // garbage type that every category roll-up then has to cope with.
  const { data: options } = useQuery<AssetFilterOptions>({
    queryKey: ['asset-filter-options'],
    queryFn: api.assetFilterOptions,
  });

  const save = useMutation({
    mutationFn: () => api.promoteCandidate(c.id, {
      name: name.trim(), device_type: type || undefined,
      attach_to_device_id: attachTo || undefined,
      // The sweep's guesses, sent only so the server can resolve them against the
      // catalog. It creates nothing from them.
      vendor: c.suggested_vendor ?? undefined,
      model: c.suggested_model ?? undefined,
    }),
    onSuccess: () => { onDone(); onClose(); },
    onError: (e) => setError(String(e)),
  });

  return (
    <Dialog title={`Promote ${c.address ?? 'responder'}`} onClose={onClose}>
      <div className="asset-form">
        {/* First, because the answer changes what everything below means: an
            attached responder inherits a name and a rack, and a new record needs
            both invented. */}
        {targets.length > 0 && (
          <label>
            <span>Fulfils</span>
            <select value={attachTo}
                    onChange={(e) => setAttachTo(e.target.value)}>
              <option value="">— a new record —</option>
              {targets.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name} ({t.lifecycle})
                  {t.rack_name ? ` · ${t.rack_name}` : ''}
                  {t.u_start ? ` U${t.u_start}` : ''}
                </option>
              ))}
            </select>
          </label>
        )}

        {/* Pre-filled from the name the device reports, and editable because a
            device naming itself is not the same as the estate's naming scheme.
            Hidden when attaching: the record already has a name, and offering to
            change it here would bury a rename inside a commissioning step. */}
        {!attachTo && (
          <label>
            <span>Name</span>
            <input value={name} autoFocus
                   onChange={(e) => setName(e.target.value)}
                   placeholder="as the estate names it" />
          </label>
        )}
        <label>
          <span>Device type</span>
          <select value={type} onChange={(e) => { setType(e.target.value);
                                                  setAttachTo(''); }}>
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
          {target ? (
            <>
              <strong>{target.name}</strong> keeps its placement
              {target.rack_name ? ` (${target.rack_name}${
                target.u_start ? ` U${target.u_start}` : ''})` : ''} and takes this
              responder's address and serial. One record, not two.
            </>
          ) : (
            <>
              A new record, <strong>installed</strong> — racked, and not accepted
              into service. Placement is not asked for: a sweep cannot know which
              rack it is in.
            </>
          )}
          {' '}Accept it from the Commissioning page once you are satisfied.
        </p>
        {error && <div className="banner">{error}</div>}
      </div>
      <DialogActions>
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button"
                disabled={(!attachTo && (!name.trim() || !type)) || save.isPending}
                onClick={() => { setError(null); save.mutate(); }}>
          {save.isPending ? 'Promoting…' : 'Promote'}
        </button>
      </DialogActions>
    </Dialog>
  );
}
