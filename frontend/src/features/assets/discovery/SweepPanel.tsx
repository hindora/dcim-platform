import { useEffect, useMemo, useState, type MouseEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  api,
  type DiscoveryRange,
  type DiscoveryRun,
  type DiscoverySchedule,
} from '../../../api/client';
import { relativeTime, untilTime } from '../../../lib/format';
import { Freezes, useBlackouts, when } from './Freezes';
import { RangeDialog } from './RangeDialog';
import { inZone, ScheduleDialog, timingLabel } from './ScheduleDialog';
import {
  collectorTrouble, lastSwept, MAX_ADDRESSES, parseCidr, probeCount, PURPOSE_LABEL,
  PURPOSE_TAG, SECONDS_PER_ADDRESS,
} from './ranges';

/** How many runs to read. Enough to say when each range was last audited, which
 *  is the question the range list exists to answer. */
const RUN_HISTORY = 30;

/** The run history, shared by the rail and the page header.
 *
 *  ONE query key with ONE query function: two components asking for
 *  ['discovery-runs'] with different limits would each overwrite the other's cache
 *  entry, and whichever rendered last would decide what both saw.
 */
export function useDiscoveryRuns() {
  return useQuery({
    queryKey: ['discovery-runs'],
    queryFn: () => api.discoveryRuns({ limit: String(RUN_HISTORY) }),
    // Fast only while something is in flight - a sweep is a background audit, not
    // an emergency. But never OFF: a schedule queues runs nobody on this page
    // started, and with polling off between sweeps the scheduled one did not
    // appear until a reload. A minute is when the scheduler ticks anyway.
    refetchInterval: (q) =>
      q.state.data?.items?.some((r) => r.status === 'pending'
        || r.status === 'running') ? 5_000 : 60_000,
  });
}

function describeDuration(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)}s`;
  return `${Math.round(seconds / 60)} min`;
}

type Editing = { range?: DiscoveryRange; cidr?: string } | null;

/** Run a sweep, and see what the last ones did.
 *
 *  This is how the DCIM learns that hardware exists: it asks the management
 *  network, over the protocols the devices actually speak, from the collector
 *  that sits on that network. It talks to no other product's API - a DCIM must not
 *  know what is generating its telemetry.
 *
 *  What it sweeps is SAVED RANGES: address space somebody said is worth auditing,
 *  with the collector that can reach it. The list used to be the /24s inventory's
 *  addresses fell in, which offered nothing at a new site, never the unrecorded
 *  subnet an audit is for, and only ever /24s. Those are now suggestions.
 *
 *  Queued, not executed here. The API records one run per collector and each
 *  collector claims its own, because the sweep happens on the management network
 *  and the API is not on it.
 */
export function SweepPanel({ activeRun, onPickRun }: {
  activeRun: string | null;
  /** Narrow the findings to what one sweep saw. */
  onPickRun: (runId: string | null) => void;
}) {
  const qc = useQueryClient();
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [extra, setExtra] = useState('');
  const [extraCollector, setExtraCollector] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<string | null>(null);
  const [allRuns, setAllRuns] = useState(false);
  const [editing, setEditing] = useState<Editing>(null);

  const rangesQ = useQuery({ queryKey: ['discovery-ranges'], queryFn: api.discoveryRanges,
                             refetchInterval: 60_000 });
  const options = useQuery({ queryKey: ['discovery-range-options'],
                             queryFn: api.discoveryRangeOptions, staleTime: 60_000 });
  const suggestQ = useQuery({ queryKey: ['discovery-subnets'],
                              queryFn: api.discoverySubnets });

  const { data: runs } = useDiscoveryRuns();
  const history = useMemo(() => runs?.items ?? [], [runs]);
  const ranges = rangesQ.data?.items ?? [];
  // A finished sweep changes what "Swept" and the audit tags say. Refetched the
  // moment one finishes, not on the next minute's poll - a range just swept by
  // Run now otherwise went on reading "never swept".
  const lastFinished = history.find((r) => r.finished_at)?.finished_at;
  useEffect(() => {
    if (lastFinished) qc.invalidateQueries({ queryKey: ['discovery-ranges'] });
  }, [lastFinished, qc]);
  const suggestions = suggestQ.data?.subnets ?? [];
  const collectors = options.data?.collectors ?? [];

  const selected = ranges.filter((r) => picked.has(r.id) && r.enabled);
  const typed = extra.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
  // Checked as it is typed. A malformed CIDR used to travel to the API to fail,
  // which is a slow way to learn you mistyped an octet.
  const parsed = typed.map(parseCidr);
  const bad = parsed.filter((p) => p.error);
  const adhoc = parsed.filter((p) => !p.error);

  // The estimate, per collector: different collectors sweep at the same time,
  // one collector sweeps its runs one after another.
  const perCollector = new Map<string, number>();
  for (const r of selected) {
    const k = r.collector_id ?? '';
    perCollector.set(k, (perCollector.get(k) ?? 0) + probeCount(r));
  }
  for (const p of adhoc) {
    perCollector.set(extraCollector, (perCollector.get(extraCollector) ?? 0) + p.addresses);
  }
  const probes = [...perCollector.values()].reduce((a, b) => a + b, 0);
  const longest = Math.max(0, ...perCollector.values());
  const runCount = [...perCollector.values()]
    .reduce((n, v) => n + Math.max(1, Math.ceil(v / MAX_ADDRESSES)), 0);
  const chosenCount = selected.length + adhoc.length;

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['discovery-runs'] });
    qc.invalidateQueries({ queryKey: ['discovery-candidates'] });
    qc.invalidateQueries({ queryKey: ['discovery-ranges'] });
    qc.invalidateQueries({ queryKey: ['discovery-subnets'] });
  };

  const blackouts = useBlackouts();
  const frozen = (blackouts.data?.items ?? []).filter((b) => b.active);
  const start = useMutation({
    // `override` only ever comes from the "Sweep anyway" button, after the API
    // has said which freeze the sweep would break.
    mutationFn: (override: boolean = false) => api.startDiscoveryRun({
      range_ids: selected.map((r) => r.id),
      subnets: adhoc.map((p) => p.cidr),
      collector_id: extraCollector || undefined,
      override_blackout: override || undefined,
    }),
    onSuccess: (r) => {
      setError(null);
      setReport(r.runs.length > 1 ? `Queued as ${r.runs.length} sweeps.` : null);
      setPicked(new Set());
      setExtra('');
      refresh();
    },
    onError: (e) => setError(String(e)),
  });

  // One click from a suggestion to a saved range, named after its CIDR until
  // somebody names it better.
  const addSuggestions = useMutation({
    mutationFn: async (cidrs: string[]) => {
      for (const cidr of cidrs) await api.createDiscoveryRange({ cidr });
    },
    onSuccess: refresh,
    onError: (e) => { setError(String(e)); refresh(); },
  });

  const toggle = (id: string) => setPicked((s) => {
    const next = new Set(s);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const sweepable = ranges.filter((r) => r.enabled);
  const stale = ranges.filter((r) => r.audit === 'overdue' || r.audit === 'never').length;
  const allPicked = sweepable.length > 0 && sweepable.every((r) => picked.has(r.id));
  const inFlight = history.filter((r) => r.status === 'pending' || r.status === 'running');
  const shownRuns = allRuns ? history : history.slice(0, 6);

  return (
    <aside className="disc-rail" aria-label="Sweep the management network">
      <section className="asset-panel disc-sweep">
        <div className="disc-rail-head">
          <h3>Sweep</h3>
          <button type="button" className="link-button"
                  onClick={() => setEditing({})}>
            Add range
          </button>
        </div>
        {frozen.map((b) => (
          <p key={b.id} className="disc-bad disc-stale">
            Change freeze{b.datacenter_name ? ` (${b.datacenter_name})` : ''} until {when(b.ends_at)}.
          </p>
        ))}
        {inFlight.map((r) => <InFlight key={r.id} run={r} onChanged={refresh} />)}

        {/* "No ranges" is a statement about the configuration; made while the list
            is still loading it was false for as long as the fetch took. */}
        {/* How many audits have stopped happening, before the list that says
            which. */}
        {stale > 0 && (
          <p className="disc-bad disc-stale">
            {stale} range{stale === 1 ? '' : 's'} overdue.
          </p>
        )}
        {rangesQ.isLoading ? (
          <div className="asset-skeleton" style={{ height: 180 }} />
        ) : ranges.length ? (
          <div className="disc-subnets">
            <table>
              <thead>
                <tr>
                  <th>
                    <input type="checkbox" checked={allPicked}
                           aria-label="Select every enabled range"
                           onChange={() => setPicked(allPicked
                             ? new Set() : new Set(sweepable.map((r) => r.id)))} />
                  </th>
                  <th>Range</th>
                  <th className="num">Known</th>
                  <th title="When a sweep last covered this range">Swept</th>
                </tr>
              </thead>
              <tbody>
                {ranges.map((r) => (
                  <RangeRow key={r.id} r={r} picked={picked.has(r.id)}
                            last={lastSwept(r.cidr, history)}
                            onToggle={() => toggle(r.id)}
                            onEdit={() => setEditing({ range: r })}
                            onDeleted={() => {
                              setPicked((s) => { const n = new Set(s); n.delete(r.id); return n; });
                              refresh();
                            }} />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="disc-empty-ranges">
            <p>No ranges yet. Use Add range.</p>
          </div>
        )}

        {/* Suggestions, not the list: inventory can only show space something is
            already recorded in, never the unrecorded subnet an audit is for.
            Collapsed by default - the count in the summary says they are there,
            and open they pushed the Run button off the rail. */}
        {suggestions.length > 0 && (
          <details className="disc-suggest">
            <summary>
              Suggested from inventory <span className="muted">· {suggestions.length}</span>
            </summary>
            <ul>
              {suggestions.map((s) => (
                <li key={s.cidr}>
                  <code>{s.cidr}</code>
                  <span className="muted">{s.known} known</span>
                  <button type="button" className="link-button"
                          disabled={addSuggestions.isPending}
                          onClick={() => addSuggestions.mutate([s.cidr])}>
                    Add
                  </button>
                </li>
              ))}
            </ul>
            {suggestions.length > 1 && (
              <button type="button" className="link-button"
                      disabled={addSuggestions.isPending}
                      onClick={() => addSuggestions.mutate(suggestions.map((s) => s.cidr))}>
                {addSuggestions.isPending ? 'Adding…' : `Add all ${suggestions.length}`}
              </button>
            )}
          </details>
        )}

        <label className="disc-extra">
          <span>Sweep once, without saving</span>
          <input value={extra} onChange={(e) => setExtra(e.target.value)}
                 placeholder="10.51.30.0/24, 10.52.30.64/26"
                 aria-invalid={bad.length > 0} />
        </label>
        {/* Only offered where there is a choice to make. */}
        {adhoc.length > 0 && collectors.length > 1 && (
          <label className="disc-extra">
            <span>Swept by</span>
            <select value={extraCollector} onChange={(e) => setExtraCollector(e.target.value)}>
              <option value="">Any collector</option>
              {collectors.map((c) => <option key={c.id} value={c.id}>{c.id}</option>)}
            </select>
          </label>
        )}
        {bad.map((p) => (
          <p key={p.cidr} className="disc-bad">
            <code>{p.cidr}</code> - {p.error}
          </p>
        ))}

        {/* What it will cost, before the button is pressed. A sweep is minutes, not
            seconds, and an operator who does not know that reads a running sweep as
            a hung page. */}
        <p className="muted disc-estimate">
          {probes > 0
            ? <>{probes.toLocaleString()} address{probes === 1 ? '' : 'es'}
                {runCount > 1 && <> in {runCount} sweeps</>} · up to{' '}
                {describeDuration(longest * SECONDS_PER_ADDRESS)}</>
            : 'Choose a range.'}
        </p>

        <button type="button" className="primary disc-run"
                disabled={chosenCount === 0 || bad.length > 0 || start.isPending}
                onClick={() => { setError(null); start.mutate(false); }}>
          {start.isPending ? 'Queueing…'
            : `Run sweep${chosenCount ? ` · ${chosenCount} range${chosenCount === 1 ? '' : 's'}` : ''}`}
        </button>

        {report && <p className="muted disc-estimate">{report}</p>}
        {error && (
          <div className="banner">
            {error}
            {/* Refused for a freeze: the override is one deliberate click, and
                the API audits it. */}
            {error.includes('change freeze') && (
              <div>
                <button type="button" className="link-button danger"
                        disabled={start.isPending}
                        onClick={() => { setError(null); start.mutate(true); }}>
                  Sweep anyway
                </button>
              </div>
            )}
          </div>
        )}
      </section>

      <Schedules chosen={selected} adhoc={adhoc.length} />

      <Freezes />

      {history.length > 0 && (
        <section className="asset-panel disc-runs">
          <h3>Recent sweeps</h3>
          {/* A sweep is a question with a date on it, so each one can narrow the
              findings to what it saw - "what did last night's run turn up" is the
              question somebody arrives with. */}
          <ol>
            {shownRuns.map((r) => (
              <RunItem key={r.id} run={r} active={r.id === activeRun}
                       onPick={() => onPickRun(r.id === activeRun ? null : r.id)} />
            ))}
          </ol>
          {history.length > 6 && (
            <button type="button" className="link-button"
                    onClick={() => setAllRuns((a) => !a)}>
              {allRuns ? 'Fewer' : `All ${history.length}`}
            </button>
          )}
        </section>
      )}

      {editing && (
        <RangeDialog range={editing.range} initialCidr={editing.cidr}
                     options={options.data} onClose={() => setEditing(null)} />
      )}
    </aside>
  );
}

function RangeRow({ r, picked, last, onToggle, onEdit, onDeleted }: {
  r: DiscoveryRange; picked: boolean;
  last: ReturnType<typeof lastSwept>;
  onToggle: () => void; onEdit: () => void; onDeleted: () => void;
}) {
  const trouble = collectorTrouble(r);
  // Two clicks, not a browser confirm(): a modal blocks the page. The second
  // click is the delete; anything else backs out.
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const remove = useMutation({
    mutationFn: () => api.deleteDiscoveryRange(r.id),
    onSuccess: onDeleted,
    // Refused while a schedule sweeps it - deleting it would quietly shrink what
    // that schedule audits - and the refusal names the schedule.
    onError: (e) => { setConfirming(false); setError(String(e)); },
  });
  const stop = (fn: () => void) => (e: MouseEvent) => { e.stopPropagation(); fn(); };
  return (
    <tr className={`${picked ? 'is-picked' : ''}${r.enabled ? '' : ' is-off'}`}
        onClick={r.enabled ? onToggle : undefined}>
      <td>
        <input type="checkbox" checked={picked} disabled={!r.enabled}
               onChange={onToggle} onClick={(e) => e.stopPropagation()}
               aria-label={`Sweep ${r.name}`} />
      </td>
      <td className="disc-range">
        <div className="top">
          <span className="name">{r.name}</span>
          <span className="acts">
            {confirming ? (
              <>
                <button type="button" className="link-button danger"
                        disabled={remove.isPending}
                        onClick={stop(() => { setError(null); remove.mutate(); })}
                        aria-label={`Confirm deleting ${r.name}`}>
                  {remove.isPending ? 'Deleting…' : 'Confirm'}
                </button>
                <button type="button" className="link-button"
                        onClick={stop(() => setConfirming(false))}>
                  Keep
                </button>
              </>
            ) : (
              <>
                <button type="button" className="link-button"
                        onClick={stop(onEdit)} aria-label={`Edit ${r.name}`}>
                  Edit
                </button>
                <button type="button" className="link-button"
                        onClick={stop(() => { setError(null); setConfirming(true); })}
                        aria-label={`Delete ${r.name}`}>
                  Delete
                </button>
              </>
            )}
          </span>
        </div>
        {r.name !== r.cidr && <code>{r.cidr}</code>}
        <div className="tags">
          {!r.enabled && <span className="proto">OFF</span>}
          <AuditTag r={r} />
          {r.purpose && (
            <span className="proto" title={PURPOSE_LABEL[r.purpose]}>
              {PURPOSE_TAG[r.purpose] ?? r.purpose}
            </span>
          )}
          {r.datacenter_name && <span className="muted">{r.datacenter_name}</span>}
          {r.collector_id && (
            <span className={trouble ? 'disc-bad' : 'muted'} title={trouble ?? undefined}>
              via {r.collector_id}
            </span>
          )}
          {r.exclusions.length > 0 && (
            <span className="muted" title={r.exclusions.join(', ')}>
              {r.exclusions.length} excl.
            </span>
          )}
        </div>
        {error && <div className="disc-bad">{error}</div>}
      </td>
      <td className="num">{r.known ?? 0}</td>
      <td className={last ? '' : 'disc-never'}>
        {last ? (
          <>
            {relativeTime(last.run.finished_at ?? last.run.started_at)}
            {!last.whole && (
              <span className="disc-part"
                    title={`Only ${(last.run.scope?.subnets ?? []).join(', ')} was swept`}>
                part
              </span>
            )}
          </>
        ) : 'never'}
      </td>
    </tr>
  );
}

/** Whether this range's audit is still happening.
 *
 *  Overdue: not fully swept within twice its schedule's gap - a paused schedule,
 *  a failing one or a collector that is gone all end here. Never: scheduled, never
 *  fully swept. Unscheduled: only swept by hand. A partial sweep does not count.
 */
function AuditTag({ r }: { r: DiscoveryRange }) {
  const last = r.last_full_at ? `last full sweep ${relativeTime(r.last_full_at)}` : 'never fully swept';
  const by = r.schedule_names?.length ? `; ${r.schedule_names.join(', ')}` : '';
  if (r.audit === 'overdue' || r.audit === 'never') {
    return (
      <span className="disc-bad" title={`${last}${by}`}>
        {r.audit === 'never' ? 'never swept' : 'overdue'}
      </span>
    );
  }
  if (r.audit === 'unscheduled') {
    return <span className="muted" title={last}>unscheduled</span>;
  }
  return null;
}

/** A sweep in flight, or one that is waiting - and on what - with a way out.
 *
 *  Cancel matters most for the stuck one: a run queued for a collector that never
 *  checks in waits for ever, and while it does it holds back every schedule,
 *  because a due schedule waits while any sweep is in flight.
 */
function InFlight({ run, onChanged }: { run: DiscoveryRun; onChanged: () => void }) {
  const trouble = run.status === 'pending' ? collectorTrouble(run) : null;
  const scope = (run.scope?.subnets ?? []).join(', ');
  const running = run.status === 'running';
  // Two clicks, not a browser confirm(): a modal blocks the page.
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cancel = useMutation({
    mutationFn: () => api.cancelDiscoveryRun(run.id),
    onSuccess: () => { setConfirming(false); onChanged(); },
    // Most likely it finished in the meantime; the refetch shows how.
    onError: (e) => { setConfirming(false); setError(String(e)); onChanged(); },
  });
  return (
    <div className={`disc-inflight${trouble ? ' is-stuck' : ''}`} role="status">
      {!trouble && <span className="disc-pulse" aria-hidden />}
      {running ? 'Sweeping' : 'Queued'} <code>{scope}</code>
      {run.collector_id && <span className="muted"> on {run.collector_id}</span>}
      <span className="muted"> · {relativeTime(run.started_at)}</span>
      {!confirming && (
        <button type="button" className="link-button disc-cancel"
                onClick={() => { setError(null); setConfirming(true); }}>
          Cancel
        </button>
      )}
      {/* Stuck, not queued: only that collector can reach the range, so nothing
          else will pick this up. */}
      {trouble && <div className="disc-bad">Waiting: {trouble}.</div>}
      {confirming && (
        <div className="disc-cancel-ask">
          {/* Said before, not after: a running sweep cannot be stopped on the
              collector, so what is being cancelled is its result. */}
          <span>
            {running ? 'Its results will be discarded.' : 'It has not started.'}
          </span>
          <button type="button" className="link-button danger"
                  disabled={cancel.isPending} onClick={() => cancel.mutate()}>
            {cancel.isPending ? 'Cancelling…' : 'Cancel sweep'}
          </button>
          <button type="button" className="link-button"
                  onClick={() => setConfirming(false)}>
            Keep
          </button>
        </div>
      )}
      {error && <div className="disc-bad">{error}</div>}
    </div>
  );
}

function RunItem({ run, active, onPick }: {
  run: DiscoveryRun; active: boolean; onPick: () => void;
}) {
  const inFlight = run.status === 'pending' || run.status === 'running';
  const secs = run.finished_at && run.started_at
    ? (Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1000 : null;
  return (
    <li className={`disc-run-item${active ? ' is-active' : ''}${
      run.status === 'failed' ? ' is-failed' : ''}`}>
      <button type="button" onClick={onPick} disabled={inFlight}
              aria-pressed={active}
              title={inFlight ? 'Still running' : active
                ? 'Show every finding again' : 'Show only what this sweep saw'}>
        <span className="when">
          {relativeTime(run.started_at)}
          {secs != null && secs >= 0 && (
            <span className="muted"> · {describeDuration(secs)}</span>
          )}
          {run.collector_id && <span className="muted"> · {run.collector_id}</span>}
          {/* Who asked. A run nobody remembers starting is a schedule's, and
              saying so saves somebody hunting the audit log for it. */}
          {(run.trigger === 'schedule' || run.schedule_id) && (
            <span className="disc-sched-tag"
                  title={run.schedule_name ? `Schedule: ${run.schedule_name}` : 'Queued by a schedule'}>
              scheduled
            </span>
          )}
        </span>
        <code className="scope">{(run.scope?.subnets ?? []).join(', ') || '—'}</code>
        {/* `status` is the API's own vocabulary (pending / running / done /
            failed) and the chip carries it as text, so a value with no colour
            still reads correctly. */}
        {run.status !== 'done' && (
          <span className={`asset-life is-${run.status}`}>{run.status}</span>
        )}
        {/* A cancelled run concluded nothing - "nothing answered" would read as
            a result it never produced. Its note says what happened instead. */}
        {run.status !== 'cancelled' && run.status !== 'skipped' && (
          <span className="result">
            {inFlight ? '…' : <Result run={run} />}
          </span>
        )}
        {/* A cancel is a decision, not a fault: muted, where a failure is red. */}
        {run.error && (
          <span className={run.status === 'cancelled' || run.status === 'skipped'
            ? 'muted disc-run-note' : 'disc-bad'}>
            {run.error}
          </span>
        )}
      </button>
    </li>
  );
}

/** What the run concluded, as a sentence.
 *
 *  "105 answered" is not a result - it says nothing about whether that is good
 *  news. "105 answered · 105 expected · 1 moved" is the audit, and it is the
 *  exceptions that get emphasis because they are the only rows anybody acts on.
 */
function Result({ run }: { run: DiscoveryRun }) {
  const found = run.found ?? 0;
  return (
    <>
      {found ? (
        <>
          {found} answered
          {run.known != null && <span className="muted"> · {run.known} expected</span>}
          {Boolean(run.unknown) && <> · <strong>{run.unknown} new</strong></>}
          {Boolean(run.moved) && <> · <strong>{run.moved} moved</strong></>}
        </>
      ) : <span className="muted">nothing answered</span>}
      <Delta run={run} />
    </>
  );
}

/** What this sweep saw differently from the one before it over the same
 *  addresses. The question a re-sweep is run to answer is "what changed", and the
 *  totals alone cannot say: 105 answered twice can be the same 105 or two
 *  different ones.
 *
 *  Absent, not zero, on runs recorded before the delta was: "no change" is a claim
 *  those runs never measured. */
function Delta({ run }: { run: DiscoveryRun }) {
  if (run.appeared == null && run.gone == null && run.changed == null) return null;
  const parts = [
    run.appeared ? `${run.appeared} appeared` : null,
    run.gone ? `${run.gone} went quiet` : null,
    run.changed ? `${run.changed} changed` : null,
  ].filter(Boolean);
  return (
    <span className="disc-delta">
      {parts.length ? parts.join(' · ') : 'no change from the sweep before'}
    </span>
  );
}

/** Ranges swept on an interval.
 *
 *  An audit run once is a snapshot; the findings only stay true if somebody keeps
 *  asking. A schedule is made from the saved ranges chosen above and points at
 *  them, so editing a range - a wider prefix, a new exclusion, another collector -
 *  changes what its schedules sweep. One-off subnets cannot be scheduled: there is
 *  no record to say which collector reaches them.
 *
 *  The API decides when: a missed tick runs once, late, rather than firing once
 *  for every interval an outage swallowed, and a due schedule waits for a sweep
 *  already running rather than stacking a second one on the management network.
 */
function Schedules({ chosen, adhoc }: { chosen: DiscoveryRange[]; adhoc: number }) {
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);
  const options = useQuery({ queryKey: ['discovery-range-options'],
                             queryFn: api.discoveryRangeOptions, staleTime: 60_000 });

  const { data, isLoading } = useQuery({
    queryKey: ['discovery-schedules'],
    queryFn: api.discoverySchedules,
    refetchInterval: 60_000,
  });
  const intervals = data?.intervals ?? [6, 12, 24, 48, 168];
  const list = data?.items ?? [];
  // The saved ranges, for the edit dialog - the same cached query the Sweep
  // table reads.
  const rangesQ = useQuery({ queryKey: ['discovery-ranges'], queryFn: api.discoveryRanges });
  const [editing, setEditing] = useState<DiscoverySchedule | null>(null);
  // The run history polls fast while a sweep is in flight; the schedule list does
  // not. Reading a schedule's last run from the history means "last: running"
  // turns into the result when the run does, not a minute later.
  const { data: runs } = useDiscoveryRuns();
  const history = runs?.items ?? [];

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['discovery-schedules'] });
    qc.invalidateQueries({ queryKey: ['discovery-runs'] });
  };

  return (
    <section className="asset-panel disc-schedules">
      <div className="disc-rail-head">
        <h3>Schedules</h3>
        {/* A dialog, not a row in the rail: time, days and a timezone do not fit
            beside a name in 324px. The ranges selected above come pre-ticked. */}
        <button type="button" className="link-button" onClick={() => setCreating(true)}>
          New schedule
        </button>
      </div>
      {isLoading ? (
        <div className="asset-skeleton" style={{ height: 60 }} />
      ) : list.length > 0 ? (
        <ul className="disc-sched-list">
          {list.map((s) => (
            <ScheduleItem key={s.id} s={s} onChanged={refresh}
                          onEdit={() => setEditing(s)}
                          lastRun={history.find((r) => r.id === s.last_run_id)} />
          ))}
        </ul>
      ) : (
        <p className="muted disc-sched-none">No schedules.</p>
      )}

      {adhoc > 0 && (
        <p className="muted disc-sched-hint">One-off subnets can't be scheduled.</p>
      )}
      {(editing || creating) && (
        <ScheduleDialog schedule={editing ?? undefined}
                        initialRangeIds={chosen.map((r) => r.id)}
                        ranges={rangesQ.data?.items ?? []} intervals={intervals}
                        options={options.data}
                        onClose={() => { setEditing(null); setCreating(false); }} />
      )}
    </section>
  );
}

function ScheduleItem({ s, onChanged, onEdit, lastRun }: {
  s: DiscoverySchedule; onChanged: () => void; onEdit: () => void;
  /** The same run as s.last_run_id, from the fresher history query. */
  lastRun?: DiscoveryRun;
}) {
  const [error, setError] = useState<string | null>(null);
  // Two clicks, not a browser confirm(): a modal dialog blocks the page, and
  // deleting a schedule is cheap to undo by making it again.
  const [confirming, setConfirming] = useState(false);
  const fail = (e: unknown) => setError(String(e));

  const toggle = useMutation({
    mutationFn: () => api.updateDiscoverySchedule(s.id, { enabled: !s.enabled }),
    onSuccess: onChanged, onError: fail,
  });
  const runNow = useMutation({
    mutationFn: () => api.updateDiscoverySchedule(s.id, { run_now: true }),
    onSuccess: onChanged, onError: fail,
  });
  const remove = useMutation({
    mutationFn: () => api.deleteDiscoverySchedule(s.id),
    onSuccess: onChanged, onError: fail,
  });

  const due = Date.parse(s.next_run_at) <= Date.now();
  // Due for over an hour and still not fired: something is holding it - its
  // collector is busy or gone. A minute or two late is just the scheduler tick.
  const lateS = (Date.now() - Date.parse(s.next_run_at)) / 1000;
  const overdue = s.enabled && lateS > 3600;
  const status = lastRun?.status ?? s.last_status;
  const finished = lastRun?.finished_at ?? s.last_finished_at;
  const found = lastRun ? lastRun.found : s.last_found;
  const unknown = lastRun ? lastRun.unknown : s.last_unknown;
  const gone = lastRun ? lastRun.gone : s.last_gone;
  const last = status === 'done' && finished
    ? `${relativeTime(finished)}${found != null ? ` · ${found} answered` : ''}`
      + (unknown ? ` · ${unknown} new` : '')
      + (gone ? ` · ${gone} went quiet` : '')
    : status ?? 'not yet';
  // Why the last one did not finish - timed out, or its collector was gone.
  const lastError = status === 'failed' || status === 'skipped' ? lastRun?.error : null;

  return (
    <li className={`disc-sched${s.enabled ? '' : ' is-paused'}`}>
      <div className="top">
        <span className="title">{s.name || s.range_names.join(', ')}</span>
        <span className="muted">{timingLabel(s)}</span>
      </div>
      {/* What it sweeps, resolved live from its ranges. */}
      <code className="scope" title={s.subnets.join(', ')}>
        {s.range_names.length ? s.range_names.join(', ') : 'no ranges - it cannot run'}
      </code>
      <div className="muted">
        {!s.enabled ? 'paused'
          : overdue ? <span className="disc-bad">overdue by {Math.round(lateS / 3600)} h</span>
            : due ? 'due now'
              // A timed run as its own zone's clock reads it - "Tue 02:00" -
              // because that is how the window was chosen.
              : s.run_at ? `next ${inZone(s.next_run_at, s.timezone)} (${untilTime(s.next_run_at)})`
                : `next ${untilTime(s.next_run_at)}`}
        {' · '}last {last}
      </div>
      {lastError && (
        <div className={status === 'skipped' ? 'muted' : 'disc-bad'}>{lastError}</div>
      )}
      <div className="acts">
        <button type="button" className="link-button" onClick={onEdit}>
          Edit
        </button>
        <button type="button" className="link-button"
                disabled={runNow.isPending || !s.enabled || due}
                title={!s.enabled ? 'Resume it first' : undefined}
                onClick={() => { setError(null); runNow.mutate(); }}>
          Run now
        </button>
        <button type="button" className="link-button" disabled={toggle.isPending}
                onClick={() => { setError(null); toggle.mutate(); }}>
          {s.enabled ? 'Pause' : 'Resume'}
        </button>
        {confirming ? (
          <>
            <button type="button" className="link-button danger"
                    disabled={remove.isPending}
                    onClick={() => { setError(null); remove.mutate(); }}>
              {remove.isPending ? 'Deleting…' : 'Confirm delete'}
            </button>
            <button type="button" className="link-button"
                    onClick={() => setConfirming(false)}>
              Keep
            </button>
          </>
        ) : (
          <button type="button" className="link-button"
                  onClick={() => setConfirming(true)}>
            Delete
          </button>
        )}
      </div>
      {error && <div className="banner">{error}</div>}
    </li>
  );
}
