import { Fragment, useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import {
  api,
  type AssetFilterOptions,
  type AttachableDevice,
  type DiscoveryCandidate,
  type MissingDevice,
} from '../../../api/client';
import { usePaged } from '../../../components/Pagination';
import { PageHead, type Kpi } from '../../../components/estate';
import { downloadCsv, stampedName } from '../../../lib/csv';
import { humanise, relativeTime } from '../../../lib/format';
import { Dialog, DialogActions } from '../components/Dialog';
import {
  changedIds, changesOf, collapse, findingOf, HARDWARE_FIELDS, ipKey, matchOf,
  memberIds, movedFrom, needsAction, type Finding, type MachineChange, type Responder,
} from './responders';
import {
  initialRows, PromoteMonitoring, rowProblem, toRequest, type MonitoringRow,
} from './PromoteMonitoring';
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
  { key: 'moved', label: 'Moved', dot: 'warning', hideWhenEmpty: true,
    tip: 'Recognised by serial at an address inventory does not expect' },
  { key: 'replaced', label: 'Replaced', dot: 'warning', hideWhenEmpty: true,
    tip: 'Answering where expected, but a serial, platform or model differs from the last sweep' },
  { key: 'missing', label: 'Missing', dot: 'warning',
    tip: 'On record, and silent to the last sweep that covered its address' },
  { key: 'changed', label: 'Changed', hideWhenEmpty: true,
    tip: 'Firmware, OS image or name differs from the last sweep - drift, not a fault' },
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
  replaced: 'Replaced',
  missing: 'Missing',
  changed: 'Changed',
  gone: 'Stopped answering',
  expected: 'Expected',
  dismissed: 'Dismissed',
  promoted: 'Promoted',
};

/** Exceptions first, then history, then the reassurance. */
const PRIORITY: Record<Finding, number> = {
  new: 0, moved: 1, replaced: 2, missing: 3, changed: 4, gone: 5,
  dismissed: 6, promoted: 7, expected: 8,
};

/** One row of the findings table: a machine that answered, or a device on record
 *  that did not. Two shapes because a missing device has no probe to show - its
 *  evidence is the record and the poller's view of it. */
type Classified =
  | { kind: 'responder'; key: string; r: Responder; f: Finding }
  | { kind: 'missing'; key: string; m: MissingDevice; f: 'missing' };

/** Whether somebody has to act. Per ROW, not per finding: a missing device in
 *  maintenance is expected to be quiet, so it is listed and not counted. */
const actionable = (x: Classified): boolean =>
  x.kind === 'missing' ? x.m.lifecycle !== 'maintenance' : needsAction(x.f);

const addressOf = (x: Classified): string | null | undefined =>
  x.kind === 'missing' ? x.m.address : x.r.addresses[0];

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
  const { data: runs, isLoading: runsLoading } = useDiscoveryRuns();
  // The other half of the reconciliation: inventory the sweep did not hear.
  const { data: missingData, isLoading: missingLoading, error: missingError } = useQuery({
    queryKey: ['discovery-missing'],
    queryFn: api.discoveryMissing,
    refetchInterval: 60_000,
  });
  const loading = isLoading || missingLoading;

  const classified = useMemo<Classified[]>(() => {
    const missing = missingData?.items ?? [];
    const missingIds = new Set(missing.map((m) => m.device_id));
    const responders: Classified[] = collapse(data?.items ?? [])
      .map((r) => ({ kind: 'responder' as const, key: r.key, r, f: findingOf(r) }))
      // One machine, one finding. A recorded device that went quiet is ALSO a
      // gone responder matched to it, and listing both counted one box twice.
      // Missing says more - the record, and whether the poller still hears it -
      // so the gone row gives way.
      .filter((x) => !(x.f === 'gone' && x.r.members.some(
        (c) => c.matched_device_id && missingIds.has(c.matched_device_id))));
    return [
      ...responders,
      ...missing.map((m) => ({
        kind: 'missing' as const, key: `missing:${m.device_id}`, m, f: 'missing' as const,
      })),
    ];
  }, [data, missingData]);

  const counts = useMemo(() => {
    const n: Record<Facet, number> = {
      all: classified.length, action: 0, new: 0, moved: 0, replaced: 0, missing: 0,
      changed: 0, gone: 0, expected: 0, dismissed: 0, promoted: 0,
    };
    for (const x of classified) {
      n[x.f] += 1;
      if (actionable(x)) n.action += 1;
    }
    return n;
  }, [classified]);

  const run = (runs?.items ?? []).find((x) => x.id === activeRun) ?? null;
  const needle = search.trim().toLowerCase();

  const visible = useMemo(() => classified
    .filter((x) => facet === 'all'
      || (facet === 'action' ? actionable(x) : x.f === facet))
    // A missing device belongs to the run that failed to hear it.
    .filter((x) => !activeRun || (x.kind === 'missing'
      ? x.m.run_id === activeRun
      : x.r.members.some((c) => c.run_id === activeRun)))
    .filter((x) => !needle || haystack(x).includes(needle))
    .sort((a, b) => PRIORITY[a.f] - PRIORITY[b.f]
      || ipKey(addressOf(a)) - ipKey(addressOf(b))),
  [classified, facet, activeRun, needle]);

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;

  // The machines that answered - what the serial coverage and the header count are
  // about. Gone, dismissed and promoted rows are history, not this sweep's result.
  const LIVE: Finding[] = ['new', 'moved', 'replaced', 'changed', 'expected'];
  const items = classified
    .flatMap((x) => (x.kind === 'responder' && LIVE.includes(x.f) ? [x.r] : []));
  const missingToAct = classified.filter((x) => x.kind === 'missing' && actionable(x)).length;
  // How much of this result can be trusted. A responder with no serial is matched
  // by address alone, so a device that has been re-addressed still reads as new.
  // Counted over machines: a server whose Redfish probe read a serial IS identified,
  // and counting its silent SNMP probe against it understated the coverage.
  const withSerial = items.filter((r) => r.serial).length;
  const serialPct = items.length ? (withSerial * 100) / items.length : null;

  const lastDone = (runs?.items ?? []).find((x) => x.status === 'done');
  const truncated = (data?.items?.length ?? 0) >= FETCH_LIMIT;

  // Absent while loading, never zero. "Needs action 0" in green before the listing
  // has arrived is the all-clear, given on no evidence - on a slow API it was on
  // screen for as long as the fetch took.
  const pending = loading ? 'loading' : null;
  // Missing replaces Stopped answering in the band. A responder nobody recorded
  // going quiet is history; a device on record going quiet is the other half of
  // the audit, and the one an operator reads first. Stopped answering keeps its
  // facet.
  const kpis: Kpi[] = [
    { caption: 'Needs action', value: pending ? null : counts.action, digits: 0,
      tone: counts.action > 0 ? 'warn' : 'ok', why: pending },
    { caption: 'Responders', value: pending ? null : items.length, digits: 0,
      why: pending },
    { caption: 'With serial', value: pending ? null : serialPct, digits: 0, unit: '%',
      why: pending ?? 'nothing has answered yet' },
    { caption: 'Missing',
      value: pending || missingError ? null : counts.missing, digits: 0,
      tone: missingToAct > 0 ? 'warn' : undefined,
      why: pending ?? (missingError ? 'the check failed' : null) },
    { caption: 'Last sweep',
      value: lastDone ? relativeTime(lastDone.finished_at ?? lastDone.started_at) : null,
      why: runsLoading ? 'loading' : 'no sweep has finished yet' },
  ];

  return (
    <div className="disc-page">
      <PageHead title="Discovery"
                sub="What answers on the management network, reconciled with inventory."
                kpis={kpis} />

      <div className="disc-layout">
        <section className="disc-main">
          <div className="disc-toolbar">
            <FindingFacets value={facet} counts={loading ? null : counts}
                           onChange={(f) => setParam('finding', f === 'action' ? null : f)} />
            {/* One group, so when the facet strip grows - every finding type that
                is present adds a cell - search and export wrap together rather
                than leaving the button alone on a line. */}
            <div className="disc-tools">
              <input type="search" className="disc-search" value={search}
                     onChange={(e) => setSearch(e.target.value)}
                     placeholder="Address, name, serial, vendor"
                     aria-label="Search responders" />
              {/* The view, not the estate: what is filtered is what is exported,
                  so the file and the screen can be compared line for line. */}
              <button type="button" className="disc-export"
                      disabled={loading || visible.length === 0}
                      title={`Download the ${visible.length.toLocaleString()} rows in this view as CSV`}
                      onClick={() => exportFindings(visible, facet)}>
                Export
              </button>
            </div>
          </div>

          {missingError && (
            <div className="banner">
              Could not check inventory against the sweeps, so devices that stopped
              answering are not listed: {String(missingError)}
            </div>
          )}

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

          {loading ? (
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
            <FindingTable rows={visible} />
          )}

          {!loading && items.length > 0 && (
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

/** Everything a search box should find a row by, across all its probes. */
function haystack(x: Classified): string {
  if (x.kind === 'missing') {
    const m = x.m;
    return [m.address, m.name, m.serial, m.device_type, m.rack_name, m.room_name,
            m.subnet].filter(Boolean).join(' ').toLowerCase();
  }
  const r = x.r;
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
  value: Facet;
  /** null while the listing is loading: a count of 0 is a claim, not a placeholder. */
  counts: Record<Facet, number> | null;
  onChange: (f: Facet) => void;
}) {
  return (
    <div className="seg facet-seg disc-facets" role="group" aria-label="Finding filter">
      {/* An empty cell hides unless it is the one pressed: a link to ?finding=moved
          must still show which filter the page is under. */}
      {FACETS.filter((o) => !o.hideWhenEmpty || (counts?.[o.key] ?? 0) > 0
                            || o.key === value).map((o) => (
        <button key={o.key} type="button"
                className={o.key === value ? 'active' : ''}
                aria-pressed={o.key === value}
                title={o.tip}
                onClick={() => onChange(o.key)}>
          {o.dot && (
            <i className={`sw ${(counts?.[o.key] ?? 0) > 0 ? o.dot : 'minor'}`} aria-hidden />
          )}
          {o.label}<b>{counts ? counts[o.key].toLocaleString() : '·'}</b>
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
          {counts.missing > 0
            ? <>, and the {counts.missing} silent device{counts.missing === 1 ? ' is' : 's are'} in
                maintenance</>
            : <>, and every device on record answered the last sweep of its address</>}
          {counts.gone > 0 && <>; {counts.gone} unrecorded responder
            {counts.gone === 1 ? '' : 's'} stopped answering</>}.
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
  if (facet === 'missing') {
    return (
      <div className="asset-empty">
        Every device on record answered the last sweep that covered its address.
        Only swept ranges are judged, and only devices polled over SNMP or Redfish:
        a chiller behind a BACnet router cannot answer a sweep however healthy it is.
      </div>
    );
  }
  if (facet === 'changed' || facet === 'replaced') {
    return (
      <div className="asset-empty">
        Nothing answered differently from the sweep before, or every difference
        has been acknowledged.
      </div>
    );
  }
  return <div className="asset-empty">Nothing in this view.</div>;
}

/** A row somebody can select: a machine to promote or dismiss, or one whose
 *  changes can be acknowledged. A missing device has neither - it is fixed on the
 *  wire or on its record, not from here. */
function selectableRow(x: Classified): x is Extract<Classified, { kind: 'responder' }> {
  if (x.kind !== 'responder') return false;
  return (x.f === 'new' && x.r.members.some((c) => c.status === 'new'))
    || changedIds(x.r).length > 0;
}

function FindingTable({ rows }: { rows: Classified[] }) {
  const paged = usePaged(rows, { noun: 'findings' });
  // Keyed by responder, not by candidate: a selection has to mean "this machine",
  // or ignoring a row would dismiss one of its protocols and leave the other.
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState<Set<string>>(new Set());

  // Only what is on the page, and only what is still actionable: a machine nobody
  // has recorded, with a probe still open. A "select all" that quietly included
  // rows the operator cannot see is how a bulk action surprises somebody - and a
  // known device has nothing to promote.
  const selectable = paged.rows.filter(selectableRow);
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
        <BulkBar keys={[...picked]} rows={rows.filter(selectableRow)}
                 onDone={() => setPicked(new Set())} />
      )}
      <div className="asset-scroll">
        <table className="disc-table">
          <thead>
            <tr>
              {bulk && (
                <th className="pick">
                  <input type="checkbox" checked={allPicked}
                         aria-label="Select every actionable responder on this page"
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
            {paged.rows.map((x) => (x.kind === 'missing' ? (
              <MissingRow key={x.key} m={x.m} bulk={bulk} />
            ) : (
              <Fragment key={x.key}>
                <Row r={x.r} f={x.f} expanded={open.has(x.key)}
                     onExpand={() => setOpen((o) => toggle(o, x.key))}
                     picked={bulk ? picked.has(x.key) : undefined}
                     selectable={selectable.some((s) => s.key === x.key)}
                     onPick={bulk ? () => setPicked((p) => toggle(p, x.key)) : undefined} />
                {open.has(x.key) && <Evidence r={x.r} cols={cols} />}
              </Fragment>
            )))}
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
  keys: string[]; rows: Extract<Classified, { kind: 'responder' }>[]; onDone: () => void;
}) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<string | null>(null);

  const picked = rows.filter((x) => keys.includes(x.key));
  // Promote and ignore are for machines nobody has recorded; acknowledge is for
  // any machine carrying a change. One selection can hold both - a firmware roll
  // across a rack that also turned up a new box.
  const chosen = picked.filter((x) => x.f === 'new').map((x) => x.r);
  const changed = picked.map((x) => x.r).filter((r) => changedIds(r).length > 0);
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
    qc.invalidateQueries({ queryKey: ['discovery-missing'] });
    qc.invalidateQueries({ queryKey: ['asset-summary'] });
    qc.invalidateQueries({ queryKey: ['commissioning-queue'] });
    onDone();
  };

  const acknowledge = useMutation({
    mutationFn: () => api.acknowledgeChanges(changed.flatMap(changedIds)),
    onSuccess: () => {
      setError(null);
      setReport(`${changed.length} acknowledged`);
      refresh();
    },
    onError: (e) => setError(String(e)),
  });

  const promote = useMutation({
    // The primary of each machine - the probe that read a serial, so the device
    // lands with the one key that survives it being re-addressed.
    mutationFn: () => api.bulkPromoteCandidates(nameable.map((r) => r.primary.id)),
    onSuccess: (r) => {
      setError(null);
      // Skipped rows are reported rather than silently dropped: an operator who
      // selected forty and promoted thirty-seven needs to know which three, and
      // why.
      // And what monitoring came with them: a bulk promote creates only the
      // endpoints the sweep proved, so the rest are counted rather than hidden.
      const made = r.promoted.reduce((n, p) => n + (p.endpoints?.length ?? 0), 0);
      const owed = r.promoted.reduce((n, p) => n + (p.endpoints_skipped?.length ?? 0), 0);
      setReport(`${r.promoted.length} promoted`
        + (r.promoted.length ? ` with ${made} endpoint${made === 1 ? '' : 's'}` : '')
        + (owed ? ` (${owed} need a credential - set them on each asset)` : '')
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
      {chosen.length > 0 && (
        <>
          <button type="button"
                  disabled={nameable.length === 0 || promote.isPending}
                  onClick={() => { setError(null); promote.mutate(); }}>
            {promote.isPending ? 'Promoting…'
              : `Promote ${nameable.length} by reported name`}
          </button>
          <button type="button" disabled={dismiss.isPending}
                  onClick={() => { setError(null); dismiss.mutate(); }}>
            {dismiss.isPending ? 'Dismissing…' : `Ignore ${chosen.length}`}
          </button>
        </>
      )}
      {/* Says it is a judgement: "these changes were expected" is recorded against
          the operator, and it is the line an investigation into a swapped box
          looks for. */}
      {changed.length > 0 && (
        <button type="button" disabled={acknowledge.isPending}
                title="Record that these changes were expected. Audited."
                onClick={() => { setError(null); acknowledge.mutate(); }}>
          {acknowledge.isPending ? 'Acknowledging…'
            : `Acknowledge ${changed.length} change${changed.length === 1 ? '' : 's'}`}
        </button>
      )}
      {nameable.length < chosen.length && (
        <span className="muted">
          {chosen.length - nameable.length} of these report no name and must be
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
  const changes = changesOf(r);

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
        {changes.length > 0 && <ChangeSummary r={r} changes={changes} />}
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
            // Each protocol in its own vocabulary. The collector files a Redfish
            // service root's Name under `sysName`, and printing it as "sysName: Root
            // Service" told an operator the BMC had answered SNMP with that.
            const redfish = c.protocol === 'redfish';
            const fields: [string, unknown][] = [
              [redfish ? 'Service root' : 'sysName', ident.sysName],
              [redfish ? 'HostName' : 'hostName', ident.hostName],
              ['sysObjectID', ident.sysObjectID], ['Model', ident.model],
              ['Vendor', ident.vendor], ['Serial', c.serial],
              ['Reached', reachedBy(c)],
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
                {/* In full, both sides. The row can only say "description
                    changed"; this is where an operator reads which firmware it
                    went from and to. */}
                {(c.changes ?? []).length > 0 && (
                  <div className="disc-changes">
                    <h4>Changed since the sweep before</h4>
                    <dl>
                      {(c.changes ?? []).map((ch) => (
                        <Fragment key={ch.id}>
                          <dt className={HARDWARE_FIELDS.has(ch.field) ? 'hw' : ''}>
                            {ch.field}
                          </dt>
                          <dd>
                            <code className="was">{ch.old ?? '—'}</code>
                            <span aria-label="became"> → </span>
                            <code>{ch.new ?? '—'}</code>
                            <span className="muted"> · {relativeTime(ch.detected_at)}</span>
                          </dd>
                        </Fragment>
                      ))}
                    </dl>
                  </div>
                )}
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
    qc.invalidateQueries({ queryKey: ['discovery-missing'] });
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

  // How it will be polled. Re-planned when the type changes: a PDU's profile is
  // its vendor's MIB and a server's SNMP agent may be its BMC, so the answer
  // depends on what the operator says the box is.
  const plan = useQuery({
    queryKey: ['monitoring-plan', c.id, type],
    queryFn: () => api.monitoringPlan(c.id, type || undefined),
    enabled: Boolean(type),
  });
  const [monitoring, setMonitoring] = useState<MonitoringRow[]>([]);
  useEffect(() => {
    if (plan.data) setMonitoring(initialRows(plan.data.endpoints));
  }, [plan.data]);
  const monitoringBlocked = monitoring.some((r) => rowProblem(r) !== null);

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
      endpoints: monitoring.filter((r) => r.include).map(toRequest),
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

        <fieldset className="asset-form-wide disc-mon-set">
          <legend>Monitoring</legend>
          {type ? (
            <PromoteMonitoring loading={plan.isLoading} rows={monitoring}
                               onChange={setMonitoring} />
          ) : (
            <p className="muted">Choose the device type first: it decides which
              agent each probe is and the profile that polls it.</p>
          )}
          {plan.error && <div className="banner">{String(plan.error)}</div>}
        </fieldset>

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
                disabled={(!attachTo && (!name.trim() || !type)) || save.isPending
                          || monitoringBlocked || plan.isLoading}
                onClick={() => { setError(null); save.mutate(); }}>
          {save.isPending ? 'Promoting…' : 'Promote'}
        </button>
      </DialogActions>
    </Dialog>
  );
}


/** What a change is called on the row. Hardware values are short and ARE the
 *  finding - "serial ABC → DEF" - so they are shown; a sysDescr is a paragraph,
 *  and the row only says it moved. The evidence row has both sides in full. */
const CHANGE_LABEL: Record<string, string> = {
  sysDescr: 'description', redfishVersion: 'Redfish version',
  hostName: 'name', sysName: 'name', sysObjectID: 'platform OID',
};

/** The change, and the one action it asks for, where the eye already is.
 *
 *  Acknowledge lives here rather than in the action column: it answers THIS line,
 *  and beside Open it pushed the table past a 1536px screen with the rail open.
 *  The new value is already on the row (the serial in its column, the text under
 *  Identity), so the row says what it WAS - the half nothing else shows.
 */
function ChangeSummary({ r, changes }: { r: Responder; changes: MachineChange[] }) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const ack = useMutation({
    mutationFn: () => api.acknowledgeChanges(changedIds(r)),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['discovery-candidates'] });
      qc.invalidateQueries({ queryKey: ['asset-summary'] });
    },
    onError: (e) => setError(String(e)),
  });
  const shown = changes.slice(0, 2);
  return (
    <div className="disc-chg">
      {shown.map((ch) => (
        <div key={`${ch.protocol}:${ch.field}`}
             className={HARDWARE_FIELDS.has(ch.field) ? 'hw' : ''}>
          {HARDWARE_FIELDS.has(ch.field) && ch.field !== 'sysObjectID' ? (
            <>{ch.field} was <code>{ch.old ?? '—'}</code></>
          ) : (
            <>{CHANGE_LABEL[ch.field] ?? ch.field} changed</>
          )}
        </div>
      ))}
      {changes.length > shown.length && (
        <div className="muted">+{changes.length - shown.length} more</div>
      )}
      <button type="button" className="link-button" disabled={ack.isPending}
              title="Record that this was expected. Audited: a changed serial somebody waved through is what an investigation looks for."
              onClick={() => { setError(null); ack.mutate(); }}>
        {ack.isPending ? 'Acknowledging…' : 'Acknowledge'}
      </button>
      {error && <div className="banner">{error}</div>}
    </div>
  );
}

/** Why a recorded device is silent, from the one other witness there is.
 *
 *  The sweep and the poller disagreeing IS the diagnosis. Polled ONLINE while the
 *  sweep heard nothing means the sweep's community or an ACL on its path, not a
 *  dead box - and sending somebody to the rack for that wastes a trip.
 */
function diagnosis(m: MissingDevice): string {
  if (m.lifecycle === 'maintenance') return 'in maintenance';
  if (m.any_online) return 'poller still hears it: sweep credentials or ACL';
  if (m.any_online === false) return 'poller cannot reach it either';
  return 'not polled';
}

/** The long form, for the tooltip and the export. */
function diagnosisDetail(m: MissingDevice): string {
  if (m.lifecycle === 'maintenance') {
    return 'In maintenance, so it may be quiet on purpose. Listed, not counted as needing action.';
  }
  if (m.any_online) {
    return 'Polled ONLINE while the sweep heard nothing: the sweep\u2019s community or '
      + 'an ACL on its path, not a dead box.';
  }
  if (m.any_online === false) {
    return `Not online to the poller either${
      m.polling_state ? ` (${m.polling_state.toLowerCase()})` : ''}: the box, its `
      + 'power or its network, not the sweep.';
  }
  return 'Not polled, so there is no second witness to compare the sweep with.';
}

function MissingRow({ m, bulk }: { m: MissingDevice; bulk: boolean }) {
  const place = [m.rack_name, m.room_name].filter(Boolean).join(' · ');
  return (
    <tr className={`f-missing${m.lifecycle === 'maintenance' ? ' is-maint' : ''}`}>
      {bulk && <td className="pick" />}
      <td className="addr">
        <span className="disc-ip">{m.address}</span>
        <div className="disc-subnet" title="The sweep that did not hear it">{m.subnet}</div>
      </td>
      <td className="finding" title={diagnosisDetail(m)}>
        <span className="label">Missing</span>
        <div className="muted">{diagnosis(m)}</div>
      </td>
      <td className="ident">
        <span className="asset-none">did not answer</span>
        <div className="descr">
          {humanise(m.device_type)}{place ? ` · ${place}` : ''}
        </div>
      </td>
      <td className="serial">
        {m.serial ?? <span className="asset-none">none on record</span>}
        {m.serial && <div className="muted">on record</div>}
      </td>
      <td>
        <Link to={`/assets/inventory/${m.device_id}`}>{m.name}</Link>
        <div className="muted">{humanise(m.lifecycle)}</div>
      </td>
      <td className="muted nowrap">
        <span className="asset-none">not heard</span>
        <div>asked {relativeTime(m.swept_at)}</div>
      </td>
      <td className="act">
        <Link to={`/assets/inventory/${m.device_id}`}>Open</Link>
      </td>
    </tr>
  );
}

/** The view as a file: every row the filters leave, not just the page on screen.
 *
 *  Built from the same classified rows the table renders, so the file and the
 *  screen cannot disagree. Text cells are formula-guarded by downloadCsv - a
 *  responder is free to call itself `=HYPERLINK(...)`.
 */
function exportFindings(rows: Classified[], facet: Facet) {
  const headers = ['Finding', 'Needs action', 'Address', 'Protocols', 'Reported name',
    'Description', 'Serial', 'Serial via', 'Inventory record', 'Matched on',
    'Recorded at', 'Lifecycle', 'Location', 'Polling', 'Changes',
    'Suggested vendor', 'Suggested type', 'Suggested model', 'Last seen', 'Swept'];
  const out = rows.map((x) => {
    if (x.kind === 'missing') {
      const m = x.m;
      return ['Missing', actionable(x) ? 'yes' : 'no', m.address, '', '', '',
        m.serial ?? '', 'record', m.name, '', m.address, m.lifecycle,
        [m.rack_name, m.room_name].filter(Boolean).join(' / '), diagnosisDetail(m), '',
        '', m.device_type, '', '', m.swept_at];
    }
    const r = x.r;
    const match = matchOf(r);
    const pick = <K extends keyof DiscoveryCandidate>(k: K) =>
      r.members.map((c) => c[k]).find(Boolean);
    const name = r.members
      .map((c) => String(c.identity?.hostName ?? c.identity?.sysName ?? '').trim())
      .find(Boolean) ?? '';
    const descr = r.members
      .map((c) => String(c.identity?.sysDescr ?? c.identity?.model ?? ''))
      .reduce((a, b) => (b.length > a.length ? b : a), '');
    const lastSeen = r.members.map((c) => c.last_seen)
      .reduce((a, b) => (String(b ?? '') > String(a ?? '') ? b : a), r.primary.last_seen);
    return [FINDING_LABEL[x.f], actionable(x) ? 'yes' : 'no', r.addresses.join(' '),
      r.protocols.join(' '), name, descr, r.serial ?? '', r.serialFrom ?? '',
      match?.matched_device_name ?? '',
      match ? (match.matched_on_serial ? 'serial' : 'address') : '',
      match?.matched_device_address ?? '', '', '', '',
      changesOf(r).map((ch) => `${ch.protocol} ${ch.field}: ${ch.old ?? ''} -> ${ch.new ?? ''}`)
        .join('; '),
      pick('suggested_vendor') ?? '', pick('suggested_device_type') ?? '',
      pick('suggested_model') ?? '', lastSeen, ''];
  });
  downloadCsv(stampedName(`discovery-${facet}`), headers, out);
}

/** How the sweep got in, in words. A reference to the credential, never the
 *  credential: the collector keeps the values and reports which one worked. */
function reachedBy(c: DiscoveryCandidate): string | null {
  const a = (c.identity?.access ?? null) as Record<string, string> | null;
  if (!a) return null;
  if (c.protocol === 'snmp') {
    const how = a.community === 'address' ? 'community = this address'
      : a.community === 'configured' ? `configured community #${a.community_index}`
        : null;
    return [`v${a.version ?? '2c'}`, a.port ? `:${a.port}` : null, how]
      .filter(Boolean).join(' · ');
  }
  const login = a.credential === 'configured' ? `configured login #${a.credential_index}`
    : a.credential === 'none' ? 'no configured login opens it' : null;
  return [a.scheme, a.port ? `:${a.port}` : null, login].filter(Boolean).join(' · ');
}

