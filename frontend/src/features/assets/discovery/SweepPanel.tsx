import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  api,
  type DiscoveryRun,
} from '../../../api/client';
import { relativeTime } from '../../../lib/format';

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
               placeholder="10.51.30.0/24, 10.52.30.0/24"
               aria-invalid={bad.length > 0} />
      </label>

      {/* Checked as it is typed. A malformed CIDR used to travel to the API to
          fail, which is a slow way to learn you mistyped an octet. */}
      {bad.map((p) => (
        <p key={p.cidr} className="muted">
          <code>{p.cidr}</code> — {p.error}
        </p>
      ))}

      {/* What it will cost, before the button is pressed. A sweep is minutes, not
          seconds, and an operator who does not know that reads a running sweep as
          a hung page. */}
      {addresses > 0 && (
        <p className="muted">
          {addresses} address{addresses === 1 ? '' : 'es'} · up to{' '}
          {describeDuration(addresses * SECONDS_PER_ADDRESS)}, and faster wherever
          devices answer — silence is what a sweep spends its time on.
        </p>
      )}

      {tooMany && (
        <div className="banner">
          {addresses} addresses is more than the {MAX_ADDRESSES} a single sweep
          will do. It is refused rather than truncated, because sweeping part of a
          range and reporting what it found would misrepresent what was audited.
          Narrow the prefix and run it in pieces.
        </div>
      )}

      <div className="asset-toolbar">
        <button type="button"
                disabled={chosen.length === 0 || bad.length > 0 || tooMany
                          || start.isPending || Boolean(inFlight)}
                onClick={() => start.mutate()}>
          {inFlight ? 'Sweep in progress…'
            : start.isPending ? 'Queueing…'
              : `Run sweep${chosen.length ? ` (${chosen.length})` : ''}`}
        </button>
        {chosen.length === 0 && bad.length === 0 && (
          <span className="muted">Choose at least one subnet.</span>
        )}
        {inFlight && (
          <span className="muted">
            Started {relativeTime(inFlight.started_at)}.
          </span>
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
                  <th>Result</th>
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
  const inFlight = run.status === 'pending' || run.status === 'running';
  return (
    <tr>
      <td className="muted">{relativeTime(run.started_at)}</td>
      <td><code>{(run.scope?.subnets ?? []).join(', ') || '—'}</code></td>
      <td>
        {/* `status` is the API's own vocabulary (pending / running / done /
            failed) and the chip carries it as text, so a value with no colour
            still reads correctly. */}
        <span className={`asset-life is-${run.status}`}>{run.status}</span>
        {run.error && <div className="muted">{run.error}</div>}
      </td>
      <td>{inFlight ? <span className="muted">…</span> : <Result run={run} />}</td>
    </tr>
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
  if (!found) return <span className="muted">nothing answered</span>;
  return (
    <>
      {found} answered
      {run.known != null && <span className="muted"> · {run.known} expected</span>}
      {Boolean(run.unknown) && <> · <strong>{run.unknown} new</strong></>}
      {Boolean(run.moved) && <> · <strong>{run.moved} moved</strong></>}
      {run.with_serial != null && (
        <div className="muted">{run.with_serial} reported a serial</div>
      )}
    </>
  );
}
