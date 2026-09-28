import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  api,
  type DiscoveryRun,
  type DiscoverySchedule,
} from '../../../api/client';
import { relativeTime, untilTime } from '../../../lib/format';

/** The sweeper's own limits, mirrored so the form can answer before the API does.
 *
 *  MaxAddresses is a refusal rather than a truncation: sweeping the first 4096 of
 *  65,536 and reporting "found 12" would be a lie about what was audited. That is
 *  the right behaviour and a bad surprise, so the form says the number first.
 */
const MAX_ADDRESSES = 4096;

/** Seconds per address at the ceiling: a SILENT address costs the full timeout
 *  once per attempt (6s x 2 attempts) and eight run at once, so 1.5s each.
 *
 *  Calibrated against real sweeps rather than guessed. A /24 of this estate - 254
 *  addresses, 105 of them answering - took 252s and 247s on two runs. The model
 *  puts the ceiling at 381s, and the gap is the 105 that answered immediately:
 *  silence is what a sweep spends its time on.
 */
const SECONDS_PER_ADDRESS = (6 * 2) / 8;

/** How many runs to read. Enough to say when each suggested range was last
 *  audited, which is the question the subnet list exists to answer. */
const RUN_HISTORY = 30;

type Parsed = { cidr: string; addresses: number; error?: string };

/** Check a CIDR and count what it would probe, without a round trip.
 *
 *  A typo used to travel to the API to fail, which is a slow way to learn you
 *  mistyped an octet.
 */
function parseCidr(raw: string): Parsed {
  const cidr = raw.trim();
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})$/.exec(cidr);
  if (!m) return { cidr, addresses: 0, error: 'not a CIDR, e.g. 10.51.11.0/24' };
  const octets = m.slice(1, 5).map(Number);
  if (octets.some((o) => o > 255)) {
    return { cidr, addresses: 0, error: 'an octet is above 255' };
  }
  const bits = Number(m[5]);
  if (bits > 32) return { cidr, addresses: 0, error: 'prefix is above /32' };
  // Minus network and broadcast, which is what the sweeper skips. A /31 and a
  // /32 have neither to skip, so they are counted whole.
  const total = 2 ** (32 - bits);
  const addresses = bits >= 31 ? total : total - 2;
  return { cidr, addresses };
}

/** [first, last] address of a CIDR as integers, or null if it is not one. */
function span(cidr: string): [number, number] | null {
  const m = /^(\d+)\.(\d+)\.(\d+)\.(\d+)\/(\d+)$/.exec(cidr.trim());
  if (!m) return null;
  const base = m.slice(1, 5).reduce((n, o) => n * 256 + Number(o), 0);
  const size = 2 ** (32 - Number(m[5]));
  const lo = base - (base % size);
  return [lo, lo + size - 1];
}

/** When a range was last looked at, and whether the look covered all of it.
 *
 *  A sweep of a /27 inside a /24 audited 30 addresses of 254. Calling the /24
 *  "swept 3 min ago" on the strength of that would be the same misstatement the
 *  address ceiling refuses to make, so a partial pass says so.
 */
function lastSwept(cidr: string, runs: DiscoveryRun[]) {
  const mine = span(cidr);
  if (!mine) return null;
  let best: { run: DiscoveryRun; whole: boolean } | null = null;
  for (const run of runs) {
    if (run.status !== 'done') continue;
    for (const s of run.scope?.subnets ?? []) {
      const theirs = span(s);
      if (!theirs || theirs[0] > mine[1] || mine[0] > theirs[1]) continue;
      const whole = theirs[0] <= mine[0] && theirs[1] >= mine[1];
      const when = run.finished_at ?? run.started_at;
      const bestWhen = best ? best.run.finished_at ?? best.run.started_at : null;
      // Newest wins; at the same instant a whole pass beats a partial one.
      if (!best || String(when) > String(bestWhen)
          || (String(when) === String(bestWhen) && whole && !best.whole)) {
        best = { run, whole };
      }
    }
  }
  return best;
}

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

/** Run a sweep, and see what the last ones did.
 *
 *  This is how the DCIM learns that hardware exists: it asks the management
 *  network, over the protocols the devices actually speak, from the collector
 *  that sits on that network. It talks to no other product's API - a DCIM must not
 *  know what is generating its telemetry.
 *
 *  Queued, not executed here. The API records the run and a collector claims it,
 *  because the sweep happens on the management network and the API is not on it.
 *
 *  A rail beside the findings rather than a form above them. The findings are what
 *  an audit is FOR; the old page put sixteen checkboxes between the operator and the
 *  first exception.
 */
export function SweepPanel({ activeRun, onPickRun }: {
  activeRun: string | null;
  /** Narrow the findings to what one sweep saw. */
  onPickRun: (runId: string | null) => void;
}) {
  const qc = useQueryClient();
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [extra, setExtra] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [allRuns, setAllRuns] = useState(false);

  const { data: subnets, isLoading: subnetsLoading } = useQuery({
    queryKey: ['discovery-subnets'],
    queryFn: api.discoverySubnets,
  });

  const { data: runs } = useDiscoveryRuns();
  const history = useMemo(() => runs?.items ?? [], [runs]);

  const typed = extra.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
  // The suggested subnets come from inventory and are /24s by construction, so
  // only what was typed can be malformed.
  const parsed = typed.map(parseCidr);
  const bad = parsed.filter((p) => p.error);
  const chosen = [...picked, ...parsed.filter((p) => !p.error).map((p) => p.cidr)];

  const addresses = [...picked].reduce((n) => n + 254, 0)
    + parsed.filter((p) => !p.error).reduce((n, p) => n + p.addresses, 0);
  const tooMany = addresses > MAX_ADDRESSES;

  const start = useMutation({
    mutationFn: () => api.startDiscoveryRun({ subnets: chosen }),
    onSuccess: () => {
      setError(null);
      setPicked(new Set());
      setExtra('');
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

  const list = subnets?.subnets ?? [];
  const allPicked = list.length > 0 && list.every((s) => picked.has(s.cidr));
  const inFlight = history.find(
    (r) => r.status === 'pending' || r.status === 'running');
  const shownRuns = allRuns ? history : history.slice(0, 6);

  return (
    <aside className="disc-rail" aria-label="Sweep the management network">
      <section className="asset-panel disc-sweep">
        <h3>Sweep</h3>
        <p className="muted disc-rail-sub">
          Bounded and deliberately slow: an unbounded sweep looks like a port scan
          to anything watching.
        </p>

        {inFlight && (
          <div className="disc-inflight" role="status">
            <span className="disc-pulse" aria-hidden />
            Sweeping <code>{(inFlight.scope?.subnets ?? []).join(', ')}</code>
            <span className="muted"> · started {relativeTime(inFlight.started_at)}</span>
          </div>
        )}

        {/* Derived from inventory rather than typed from memory, and each one says
            when it was last audited - which is what decides whether to sweep it. */}
        {/* "No management addresses in inventory" is a statement about the estate;
            made while the list is still loading it was false for as long as the
            fetch took. */}
        {subnetsLoading ? (
          <div className="asset-skeleton" style={{ height: 180 }} />
        ) : list.length ? (
          <div className="disc-subnets">
            <table>
              <thead>
                <tr>
                  <th>
                    <input type="checkbox" checked={allPicked}
                           aria-label="Select every suggested subnet"
                           onChange={() => setPicked(allPicked
                             ? new Set() : new Set(list.map((s) => s.cidr)))} />
                  </th>
                  <th>Subnet</th>
                  <th className="num">Known</th>
                  <th>Last swept</th>
                </tr>
              </thead>
              <tbody>
                {list.map((s) => {
                  const last = lastSwept(s.cidr, history);
                  return (
                    <tr key={s.cidr} className={picked.has(s.cidr) ? 'is-picked' : ''}
                        onClick={() => toggle(s.cidr)}>
                      <td>
                        <input type="checkbox" checked={picked.has(s.cidr)}
                               onChange={() => toggle(s.cidr)}
                               onClick={(e) => e.stopPropagation()}
                               aria-label={`Sweep ${s.cidr}`} />
                      </td>
                      <td><code>{s.cidr}</code></td>
                      <td className="num">{s.known}</td>
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
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted">
            No management addresses in inventory yet, so there is nothing to suggest.
            Enter a subnet below.
          </p>
        )}

        <label className="disc-extra">
          <span>Other subnets</span>
          <input value={extra} onChange={(e) => setExtra(e.target.value)}
                 placeholder="10.51.30.0/24, 10.52.30.0/24"
                 aria-invalid={bad.length > 0} />
        </label>

        {/* Checked as it is typed. A malformed CIDR used to travel to the API to
            fail, which is a slow way to learn you mistyped an octet. */}
        {bad.map((p) => (
          <p key={p.cidr} className="disc-bad">
            <code>{p.cidr}</code> — {p.error}
          </p>
        ))}

        {/* What it will cost, before the button is pressed. A sweep is minutes, not
            seconds, and an operator who does not know that reads a running sweep as
            a hung page. */}
        <p className="muted disc-estimate">
          {addresses > 0
            ? <>{addresses.toLocaleString()} address{addresses === 1 ? '' : 'es'} · up
                to {describeDuration(addresses * SECONDS_PER_ADDRESS)}, faster wherever
                devices answer.</>
            : 'Choose at least one subnet.'}
        </p>

        {tooMany && (
          <div className="banner">
            {addresses.toLocaleString()} addresses is more than the {MAX_ADDRESSES} a
            single sweep will do. It is refused rather than truncated, because
            sweeping part of a range and reporting what it found would misrepresent
            what was audited. Run it in pieces.
          </div>
        )}

        <button type="button" className="primary disc-run"
                disabled={chosen.length === 0 || bad.length > 0 || tooMany
                          || start.isPending || Boolean(inFlight)}
                onClick={() => start.mutate()}>
          {inFlight ? 'Sweep in progress…'
            : start.isPending ? 'Queueing…'
              : `Run sweep${chosen.length ? ` · ${chosen.length} subnet${chosen.length === 1 ? '' : 's'}` : ''}`}
        </button>

        {error && <div className="banner">{error}</div>}
      </section>

      <Schedules chosen={chosen} blocked={bad.length > 0 || tooMany} />

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
    </aside>
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
        <span className="result">
          {inFlight ? '…' : <Result run={run} />}
        </span>
        {run.error && <span className="disc-bad">{run.error}</span>}
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

const INTERVAL_LABEL: Record<number, string> = {
  6: 'every 6 hours', 12: 'every 12 hours', 24: 'daily', 48: 'every 2 days',
  168: 'weekly',
};
const intervalLabel = (h: number) => INTERVAL_LABEL[h] ?? `every ${h} hours`;

/** Ranges swept on an interval.
 *
 *  An audit run once is a snapshot; the findings only stay true if somebody keeps
 *  asking. A schedule is made from the subnets chosen in the sweep form above, so
 *  there is one way to say which ranges and it is the one already in front of the
 *  operator.
 *
 *  The API decides when: a missed tick runs once, late, rather than firing once
 *  for every interval an outage swallowed, and a due schedule waits for a sweep
 *  already running rather than stacking a second one on the management network.
 */
function Schedules({ chosen, blocked }: { chosen: string[]; blocked: boolean }) {
  const qc = useQueryClient();
  const [hours, setHours] = useState(24);
  const [name, setName] = useState('');
  const [error, setError] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ['discovery-schedules'],
    queryFn: api.discoverySchedules,
    refetchInterval: 60_000,
  });
  const intervals = data?.intervals ?? [6, 12, 24, 48, 168];
  const list = data?.items ?? [];
  // The run history polls fast while a sweep is in flight; the schedule list does
  // not. Reading a schedule's last run from the history means "last: running"
  // turns into the result when the run does, not a minute later.
  const { data: runs } = useDiscoveryRuns();
  const history = runs?.items ?? [];

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['discovery-schedules'] });
    qc.invalidateQueries({ queryKey: ['discovery-runs'] });
  };

  const create = useMutation({
    mutationFn: () => api.createDiscoverySchedule({
      subnets: chosen, interval_hours: hours, name: name.trim() || undefined,
    }),
    onSuccess: () => { setError(null); setName(''); refresh(); },
    onError: (e) => setError(String(e)),
  });

  return (
    <section className="asset-panel disc-schedules">
      <h3>Schedules</h3>
      <p className="muted disc-rail-sub">
        Sweeps that run themselves. One that comes due during another sweep waits
        for it; one missed while the platform was down runs once, late.
      </p>

      {isLoading ? (
        <div className="asset-skeleton" style={{ height: 60 }} />
      ) : list.length > 0 ? (
        <ul className="disc-sched-list">
          {list.map((s) => (
            <ScheduleItem key={s.id} s={s} onChanged={refresh}
                          lastRun={history.find((r) => r.id === s.last_run_id)} />
          ))}
        </ul>
      ) : (
        <p className="muted disc-sched-none">
          Nothing is swept on a schedule, so every finding here is only as fresh as
          the last time somebody ran one.
        </p>
      )}

      <div className="disc-sched-new">
        <select value={hours} aria-label="How often"
                onChange={(e) => setHours(Number(e.target.value))}>
          {intervals.map((h) => <option key={h} value={h}>{intervalLabel(h)}</option>)}
        </select>
        <input value={name} onChange={(e) => setName(e.target.value)}
               placeholder="Name (optional)" aria-label="Schedule name" />
        <button type="button"
                disabled={chosen.length === 0 || blocked || create.isPending}
                title={chosen.length === 0 ? 'Choose subnets in the sweep form first' : undefined}
                onClick={() => { setError(null); create.mutate(); }}>
          {create.isPending ? 'Saving…'
            : chosen.length
              ? `Schedule ${chosen.length} subnet${chosen.length === 1 ? '' : 's'}`
              : 'Schedule'}
        </button>
      </div>
      {chosen.length === 0 && (
        <p className="muted disc-sched-hint">
          Choose subnets in the sweep form above to schedule them.
        </p>
      )}
      {error && <div className="banner">{error}</div>}
    </section>
  );
}

function ScheduleItem({ s, onChanged, lastRun }: {
  s: DiscoverySchedule; onChanged: () => void;
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

  return (
    <li className={`disc-sched${s.enabled ? '' : ' is-paused'}`}>
      <div className="top">
        <span className="title">{s.name || s.subnets.join(', ')}</span>
        <span className="muted">{intervalLabel(s.interval_hours)}</span>
      </div>
      {s.name && <code className="scope">{s.subnets.join(', ')}</code>}
      <div className="muted">
        {!s.enabled ? 'paused'
          : due ? 'due now - starts within a minute'
            : `next ${untilTime(s.next_run_at)}`}
        {' · '}last {last}
      </div>
      <div className="acts">
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
