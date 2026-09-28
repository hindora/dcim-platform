import { Fragment, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import {
  api,
  type AssetFilterOptions,
  type AttachableDevice,
  type DiscoveryCandidate,
} from '../../../api/client';
import { usePaged } from '../../../components/Pagination';
import { PageHead, type Kpi } from '../../../components/estate';
import { humanise, relativeTime } from '../../../lib/format';
import { Dialog, DialogActions } from '../components/Dialog';
import {
  collapse, findingOf, ipKey, matchOf, memberIds, movedFrom, needsAction,
  type Finding, type Responder,
} from './responders';
import { SweepPanel, useDiscoveryRuns } from './SweepPanel';
import './discovery.css';

/** The API's own ceiling for one listing. Asked for in full so the page's totals are
 *  the estate's, and SAID when it is hit rather than quietly undercounting. */
const FETCH_LIMIT = 1000;

/** The facet strip's cells, in reading order. `action` is the default view. */
type Facet = 'all' | 'action' | Finding;

const FACETS: { key: Facet; label: string; dot?: 'warning' | 'minor';
               hideWhenEmpty?: boolean; tip: string }[] = [
  { key: 'all', label: 'All', tip: 'Every responder, whatever the audit concluded' },
  { key: 'action', label: 'Needs action', dot: 'warning',
    tip: 'Not in inventory, or recognised at an address inventory does not expect' },
  { key: 'new', label: 'Not in inventory', dot: 'warning',
    tip: 'Answering, and no record accounts for it' },
  { key: 'moved', label: 'Moved', dot: 'warning',
    tip: 'Recognised by serial at an address inventory does not expect' },
  { key: 'gone', label: 'Stopped answering', dot: 'minor',
    tip: 'Answered once, and not on the last sweep that covered its address' },
  { key: 'expected', label: 'Expected',
    tip: 'Answering where inventory says it is' },
  { key: 'dismissed', label: 'Dismissed', hideWhenEmpty: true,
    tip: 'Somebody decided these were not worth recording' },
  { key: 'promoted', label: 'Promoted', hideWhenEmpty: true,
    tip: 'Turned into inventory records by an operator' },
];

const FINDING_LABEL: Record<Finding, string> = {
  new: 'Not in inventory',
  moved: 'Moved',
  gone: 'Stopped answering',
  expected: 'Expected',
  dismissed: 'Dismissed',
  promoted: 'Promoted',
};

/** Exceptions first, then history, then the reassurance. */
const PRIORITY: Record<Finding, number> = {
  new: 0, moved: 1, gone: 2, dismissed: 3, promoted: 4, expected: 5,
};

type Classified = { r: Responder; f: Finding };

/** Discovery: what answered on the management network, reconciled with inventory.
 *
 *  The value of an audit is the EXCEPTION, so the page opens on what needs action
 *  and says plainly when nothing does - with the denominator, because "all 177 where
 *  we expected them" is the reassurance. The expected rows are one click away, not
 *  in the way.
 *
 *  One table, one finding per machine. It used to be four sections fed by two
 *  fetches - the main one carried every status, so a dismissed responder appeared in
 *  its bucket AND in the Dismissed list.
 *
 *  Promote asks for the name, because discovery cannot know it - a sysDescr regex is
 *  not authority to name an inventory record. It deliberately does NOT ask for
 *  placement: a sweep cannot know which rack a box is in, and guessing would put a
 *  wrong U in the elevation. The device lands as `installed` - racked, and not
 *  accepted by anybody - and then appears on the Commissioning page.
 */
export function CandidateQueue() {
  const [params, setParams] = useSearchParams();
  const facet = (params.get('finding') as Facet | null) ?? 'action';
  const activeRun = params.get('run');
  const [search, setSearch] = useState('');

  // In the URL, like the estate pages' drill state: a link to "what last night's
  // sweep found" is worth being able to send somebody.
  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value); else next.delete(key);
    setParams(next, { replace: true });
  };

  // Every status in one listing, classified client-side. Declared BEFORE the error
  // return: a hook after an early return is called conditionally, so the first
  // failed fetch would change the hook count between renders and React would throw
  // - hiding the real error behind a crash.
  const { data, isLoading, error } = useQuery<{ items: DiscoveryCandidate[] }>({
    queryKey: ['discovery-candidates'],
    queryFn: () => api.discoveryCandidates({ limit: String(FETCH_LIMIT) }),
    refetchInterval: 60_000,
  });
  const { data: runs } = useDiscoveryRuns();

  const classified = useMemo<Classified[]>(
    () => collapse(data?.items ?? []).map((r) => ({ r, f: findingOf(r) })),
    [data]);

  const counts = useMemo(() => {
    const n: Record<Facet, number> = {
      all: classified.length, action: 0, new: 0, moved: 0, gone: 0,
      expected: 0, dismissed: 0, promoted: 0,
    };
    for (const { f } of classified) {
      n[f] += 1;
      if (needsAction(f)) n.action += 1;
    }
    return n;
  }, [classified]);

  const run = (runs?.items ?? []).find((x) => x.id === activeRun) ?? null;
  const needle = search.trim().toLowerCase();

  const visible = useMemo(() => classified
    .filter(({ f }) => facet === 'all'
      || (facet === 'action' ? needsAction(f) : f === facet))
    .filter(({ r }) => !activeRun || r.members.some((c) => c.run_id === activeRun))
    .filter(({ r }) => !needle || haystack(r).includes(needle))
    .sort((a, b) => PRIORITY[a.f] - PRIORITY[b.f]
      || ipKey(a.r.addresses[0]) - ipKey(b.r.addresses[0])),
  [classified, facet, activeRun, needle]);

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;

  // The machines that answered - what the serial coverage and the header count are
  // about. Gone, dismissed and promoted rows are history, not this sweep's result.
  const items = classified
    .filter(({ f }) => f === 'new' || f === 'moved' || f === 'expected')
    .map(({ r }) => r);
  // How much of this result can be trusted. A responder with no serial is matched
  // by address alone, so a device that has been re-addressed still reads as new.
  // Counted over machines: a server whose Redfish probe read a serial IS identified,
  // and counting its silent SNMP probe against it understated the coverage.
  const withSerial = items.filter((r) => r.serial).length;
  const serialPct = items.length ? (withSerial * 100) / items.length : null;

  const lastDone = (runs?.items ?? []).find((x) => x.status === 'done');
  const truncated = (data?.items?.length ?? 0) >= FETCH_LIMIT;

  const kpis: Kpi[] = [
    { caption: 'Needs action', value: counts.action, digits: 0,
      tone: counts.action > 0 ? 'warn' : 'ok' },
    { caption: 'Responders', value: items.length, digits: 0 },
    { caption: 'With serial', value: serialPct, digits: 0, unit: '%',
      why: 'nothing has answered yet' },
    { caption: 'Stopped answering', value: counts.gone, digits: 0 },
    { caption: 'Last sweep',
      value: lastDone ? relativeTime(lastDone.finished_at ?? lastDone.started_at) : null,
      why: 'no sweep has finished yet' },
  ];

  return (
    <div className="disc-page">
      <PageHead title="Discovery"
                sub="What answers on the management network, reconciled with inventory."
                kpis={kpis} />

      <div className="disc-layout">
        <section className="disc-main">
          <div className="disc-toolbar">
            <FindingFacets value={facet} counts={counts}
                           onChange={(f) => setParam('finding', f === 'action' ? null : f)} />
            <input type="search" className="disc-search" value={search}
                   onChange={(e) => setSearch(e.target.value)}
                   placeholder="Address, name, serial, vendor"
                   aria-label="Search responders" />
          </div>

          {run && (
            <div className="disc-runfilter">
              Responders last seen by the sweep of{' '}
              <code>{(run.scope?.subnets ?? []).join(', ')}</code>,{' '}
              {relativeTime(run.started_at)}.{' '}
              <button type="button" className="link-button"
                      onClick={() => setParam('run', null)}>
                Show every sweep
              </button>
            </div>
          )}

          {truncated && (
            <div className="banner">
              Showing the first {FETCH_LIMIT.toLocaleString()} probes. Responders not in
              inventory are listed first, so no exception is hidden - but the totals on
              this page undercount the estate.
            </div>
          )}

          {isLoading ? (
            <div className="asset-skeleton" style={{ height: 240 }} />
          ) : classified.length === 0 ? (
            <div className="asset-empty">
              Nothing has answered yet. Choose subnets in the sweep panel and run it:
              every address is asked whether something is there, and what replies is
              staged here for you to promote into inventory or dismiss.
            </div>
          ) : visible.length === 0 ? (
            <Empty facet={facet} counts={counts} searching={Boolean(needle)}
                   onShow={(f) => setParam('finding', f)} />
          ) : (
            <ResponderTable rows={visible} />
          )}

          {items.length > 0 && (
            <p className="muted disc-note">
              {withSerial} of {items.length} responders reported a serial.
              {withSerial < items.length && (
                <> The other {items.length - withSerial} are matched by address
                alone, so one that has been re-addressed will read as new.</>
              )}
            </p>
          )}
        </section>

        <SweepPanel activeRun={activeRun}
                    onPickRun={(id) => setParam('run', id)} />
      </div>
    </div>
  );
}

/** Everything a search box should find a machine by, across all its probes. */
function haystack(r: Responder): string {
  const m = matchOf(r);
  return [
    ...r.addresses, r.serial, m?.matched_device_name,
    ...r.members.flatMap((c) => [
      c.suggested_vendor, c.suggested_device_type,
      String(c.identity?.sysName ?? ''), String(c.identity?.hostName ?? ''),
    ]),
  ].filter(Boolean).join(' ').toLowerCase();
}

/** The finding filter: one strip, one pressed cell, the count inside each.
 *
 *  The segmented control every filter in the product wears, not pill chips. Counts
 *  are always the whole population, so a pressed cell can be compared with its
 *  neighbours. A hue dot rides inside the cell where the finding IS a state that
 *  wants attention; the control's own colour only ever means "pressed".
 */
function FindingFacets({ value, counts, onChange }: {
  value: Facet; counts: Record<Facet, number>; onChange: (f: Facet) => void;
}) {
  return (
    <div className="seg facet-seg disc-facets" role="group" aria-label="Finding filter">
      {FACETS.filter((o) => !o.hideWhenEmpty || counts[o.key] > 0).map((o) => (
        <button key={o.key} type="button"
                className={o.key === value ? 'active' : ''}
                aria-pressed={o.key === value}
                title={o.tip}
                onClick={() => onChange(o.key)}>
          {o.dot && <i className={`sw ${counts[o.key] > 0 ? o.dot : 'minor'}`} aria-hidden />}
          {o.label}<b>{counts[o.key].toLocaleString()}</b>
        </button>
      ))}
    </div>
  );
}

/** An empty view says WHY it is empty. "Needs action: 0" is the good news, and it
 *  comes with the denominator and a way to see the rows it is vouching for. */
function Empty({ facet, counts, searching, onShow }: {
  facet: Facet; counts: Record<Facet, number>; searching: boolean;
  onShow: (f: Facet) => void;
}) {
  if (searching) {
    return <div className="asset-empty">No responder in this view matches the search.</div>;
  }
  if (facet === 'action') {
    return (
      <div className="disc-allclear">
        <span className="tick" aria-hidden>✓</span>
        <div>
          <strong>Nothing needs action.</strong>{' '}
          All {counts.expected.toLocaleString()} responder
          {counts.expected === 1 ? ' is' : 's are'} where inventory expects them
          {counts.gone > 0 && <>, and {counts.gone} stopped answering</>}.
          <div className="actions">
            <button type="button" className="link-button" onClick={() => onShow('expected')}>
              Show the {counts.expected.toLocaleString()} expected
            </button>
            {counts.gone > 0 && (
              <button type="button" className="link-button" onClick={() => onShow('gone')}>
                Show what stopped answering
              </button>
            )}
          </div>
        </div>
      </div>
    );
  }
  return <div className="asset-empty">Nothing in this view.</div>;
}

function ResponderTable({ rows }: { rows: Classified[] }) {
  const paged = usePaged(rows, { noun: 'responders' });
  // Keyed by responder, not by candidate: a selection has to mean "this machine",
  // or ignoring a row would dismiss one of its protocols and leave the other.
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState<Set<string>>(new Set());

  // Only what is on the page, and only what is still actionable: a machine nobody
  // has recorded, with a probe still open. A "select all" that quietly included
  // rows the operator cannot see is how a bulk action surprises somebody - and a
  // known device has nothing to promote.
  const selectable = paged.rows.filter(
    (row) => row.f === 'new' && row.r.members.some((c) => c.status === 'new'));
  const bulk = selectable.length > 0;
  const allPicked = bulk && selectable.every(({ r }) => picked.has(r.key));

  const toggle = (set: Set<string>, key: string) => {
    const next = new Set(set);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  };
  const cols = bulk ? 8 : 7;

  return (
    <>
      {bulk && (
        <BulkBar keys={[...picked]} rows={rows.map(({ r }) => r)}
                 onDone={() => setPicked(new Set())} />
      )}
      <div className="asset-scroll">
        <table className="disc-table">
          <thead>
            <tr>
              {bulk && (
                <th className="pick">
                  <input type="checkbox" checked={allPicked}
                         aria-label="Select every unrecorded responder on this page"
                         onChange={() => setPicked((p) => {
                           const next = new Set(p);
                           if (allPicked) selectable.forEach(({ r }) => next.delete(r.key));
                           else selectable.forEach(({ r }) => next.add(r.key));
                           return next;
                         })} />
                </th>
              )}
              <th>Address</th><th>Finding</th><th>Identity</th><th>Serial</th>
              <th>Inventory</th><th>Last seen</th>
              <th className="act">Action</th>
            </tr>
          </thead>
          <tbody>
            {paged.rows.map(({ r, f }) => (
              <Fragment key={r.key}>
                <Row r={r} f={f} expanded={open.has(r.key)}
                     onExpand={() => setOpen((o) => toggle(o, r.key))}
                     picked={bulk ? picked.has(r.key) : undefined}
                     selectable={selectable.some((s) => s.r.key === r.key)}
                     onPick={bulk ? () => setPicked((p) => toggle(p, r.key)) : undefined} />
                {open.has(r.key) && <Evidence r={r} cols={cols} />}
              </Fragment>
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
    return report ? <p className="muted disc-report">{report}</p> : null;
  }

  return (
    <div className="asset-toolbar disc-bulk">
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

function Row({ r, f, expanded, onExpand, picked, selectable, onPick }: {
  r: Responder; f: Finding; expanded: boolean; onExpand: () => void;
  picked?: boolean; selectable: boolean; onPick?: () => void;
}) {
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
  const suggestion = [pick('suggested_vendor'),
    pick('suggested_device_type') ? humanise(String(pick('suggested_device_type'))) : null,
    pick('suggested_model')].filter(Boolean).join(' · ');

  return (
    <tr className={`f-${f}${expanded ? ' is-open' : ''}`}>
      {onPick && (
        <td className="pick">
          {/* Only an actionable row is selectable: a promoted one has nothing left
              to do to it, and including it would make a count that lies. */}
          {selectable && (
            <input type="checkbox" checked={Boolean(picked)} onChange={onPick}
                   aria-label={`Select ${r.addresses[0] ?? 'responder'}`} />
          )}
        </td>
      )}
      <td className="addr">
        <button type="button" className="disc-expand" aria-expanded={expanded}
                aria-label={`${expanded ? 'Hide' : 'Show'} the evidence for ${
                  r.addresses[0] ?? 'this responder'}`}
                onClick={onExpand}>
          <span className="chev" aria-hidden>{expanded ? '▾' : '▸'}</span>
          <span className="ip">
            {r.addresses.length > 0
              ? r.addresses.map((a) => <span key={a}>{a}</span>)
              : '—'}
          </span>
        </button>
        {/* Which protocols reached it. Two tags on one row is the answer to why the
            Serial column can be populated for a machine whose SNMP agent publishes
            no serial OID. */}
        <div className="protos" aria-label={`Answered on ${r.protocols.join(' · ')}`}>
          {r.protocols.map((p) => <span key={p} className="proto">{p}</span>)}
        </div>
      </td>
      <td className="finding">
        <span className="label">{FINDING_LABEL[f]}</span>
        {from && <div className="muted">recorded at {from}</div>}
        {f === 'gone' && <div className="muted">last answered {relativeTime(lastSeen)}</div>}
      </td>
      <td className="ident">
        <Identity r={r} />
        {/* The sweep's guess, under the evidence it was guessed from - and only
            where there is no record. A column of its own pushed the table past a
            1536px screen with the sweep rail open; on a device inventory already
            knows, the record is the authority and the guess is a third line of
            noise on every one of 177 rows. */}
        {suggestion && !match?.matched_device_id && (
          <div className="guess">suggests {suggestion}</div>
        )}
      </td>
      <td className="serial">
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
            </div>
          </>
        ) : (
          <span className="asset-none">no record</span>
        )}
      </td>
      <td className="muted nowrap">{relativeTime(lastSeen)}</td>
      <td className="act"><CandidateActions r={r} /></td>
    </tr>
  );
}

/** What the machine calls itself, and the first line of what it says it is.
 *
 *  sysDescr is the whole evidence for the suggested type, so it is never cut off for
 *  good: the row's expander shows every probe's full text. That replaced a `title`
 *  tooltip, which is unreachable by keyboard and invisible on a touch screen.
 */
function Identity({ r }: { r: Responder }) {
  const name = r.members
    .map((c) => String(c.identity?.hostName ?? c.identity?.sysName ?? '').trim())
    .find(Boolean);
  // The fullest description any probe returned. A Redfish service root carries a
  // version and a name and no model at all, so taking the primary's would often
  // throw away the sysDescr that is the evidence for the suggested type.
  const descr = r.members
    .map((m) => String(m.identity?.sysDescr ?? m.identity?.model ?? ''))
    .reduce((a, b) => (b.length > a.length ? b : a), '');
  if (!name && !descr) return <span className="asset-none">nothing reported</span>;
  return (
    <>
      {name && <div className="name">{name}</div>}
      {descr && <div className="descr">{descr}</div>}
    </>
  );
}

/** Every probe that reached the machine, in full.
 *
 *  The row is a summary built from several probes; this is the evidence it was built
 *  from - which protocol said what, when, and in which sweep - because an operator
 *  deciding whether to promote a box should be able to see exactly what it said.
 */
function Evidence({ r, cols }: { r: Responder; cols: number }) {
  return (
    <tr className="disc-evidence">
      <td colSpan={cols}>
        <div className="grid">
          {r.members.map((c) => {
            const ident = c.identity ?? {};
            const fields: [string, unknown][] = [
              ['sysName', ident.sysName], ['hostName', ident.hostName],
              ['sysObjectID', ident.sysObjectID], ['Model', ident.model],
              ['Vendor', ident.vendor], ['Serial', c.serial],
            ];
            return (
              <div key={c.id} className="probe">
                <div className="head">
                  <span className="proto">{c.protocol.toUpperCase()}</span>
                  <code>{c.address ?? '—'}</code>
                  <span className={`asset-life is-${c.status}`}>{humanise(c.status)}</span>
                  <span className="muted">
                    first {relativeTime(c.first_seen)} · last {relativeTime(c.last_seen)}
                  </span>
                </div>
                {ident.sysDescr != null && (
                  <p className="full">{String(ident.sysDescr)}</p>
                )}
                <dl>
                  {fields.filter(([, v]) => v != null && String(v).trim()).map(([k, v]) => (
                    <Fragment key={k}>
                      <dt>{k}</dt><dd><code>{String(v)}</code></dd>
                    </Fragment>
                  ))}
                </dl>
              </div>
            );
          })}
        </div>
      </td>
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
function CandidateActions({ r }: { r: Responder }) {
  const c = r.primary;
  const qc = useQueryClient();
  const [naming, setNaming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    // Keyed prefix, so every listing of candidates refetches together.
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
  // A dismissed machine: every probe on it carries status: 'ignored', and the only
  // honest action is to reverse the decision.
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
    <div className="disc-actions">
      <button type="button" className="primary" onClick={() => setNaming(true)}>
        Promote
      </button>
      <button type="button" disabled={dismiss.isPending}
              onClick={() => { setError(null); dismiss.mutate(); }}>
        Ignore
      </button>
      {error && <div className="banner">{error}</div>}
      {naming && (
        <PromoteDialog c={c} onClose={() => setNaming(false)} onDone={refresh} />
      )}
    </div>
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

        {/* Shown, not silently applied. The vendor and model are matched against
            the catalog on promotion and never created from - so an unrecognised
            one is left for the operator rather than becoming "DELL" beside
            "Dell Inc.". This sentence used to say they were not recorded at all,
            which stopped being true when the catalog lookup landed. */}
        {(c.suggested_vendor || c.suggested_model || c.serial) && (
          <p className="muted asset-form-wide">
            The sweep read:{' '}
            {[c.suggested_vendor, c.suggested_model,
              c.serial ? `serial ${c.serial}` : null]
              .filter(Boolean).join(' · ')}.
            {(c.suggested_vendor || c.suggested_model) && (
              <> Vendor and model are matched against the catalog; one it does not
              recognise is left for you to set on the asset.</>
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
        <button type="button" className="primary"
                disabled={(!attachTo && (!name.trim() || !type)) || save.isPending}
                onClick={() => { setError(null); save.mutate(); }}>
          {save.isPending ? 'Promoting…' : 'Promote'}
        </button>
      </DialogActions>
    </Dialog>
  );
}
