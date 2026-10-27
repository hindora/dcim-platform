import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ApiError, FINDING_STATES, api, atLeast,
  type Alarm, type AnomalyFindingDetail, type DismissReason, type FeedbackAction,
  type FindingSignal,
} from '../../api/client';
import { MaxGlyph, MaxModal } from '../../components/MaxModal';
import { TimeChart } from '../../components/TimeChart';
import { metricLabel, relativeTime } from '../../lib/format';
import { METRICS, type MetricKey } from '../../lib/metrics.gen';
import { Dialog, DialogActions } from '../assets/components/Dialog';
import { frameFinding, useAnomalyLive } from '../analytics/anomaly/anomalyEvents';
import {
  DISMISS_REASONS, KIND_WORD, LAYER_NAME, RESOLUTION_WORD, STATE_WORD,
  duration, num, signed, stamp,
} from '../analytics/anomaly/format';
import '../analytics/anomaly.css';
import { Notice } from '../../components/Notice';

/** The evidence behind an alarm that analysis raised (doc 31 section 8).
 *
 *  Only for `detection = 'derived'` and `source = 'analytics'`: every other
 *  alarm renders exactly as before. A derived alarm is the alert lane of a
 *  finding, and the finding is where the reasoning lives - which rule, what
 *  it read, how far past its limit and for how long - so this panel fetches
 *  the finding behind the alarm and prints that.
 *
 *  Acknowledge is NOT offered here. The alarm is authoritative (doc 30
 *  section 4.4): its own Acknowledge button acknowledges the finding within
 *  one sweep, so a second button would be two names for one act. What the
 *  finding adds is the verdict - Confirm fault, Dismiss with a reason, or
 *  Reopen - because that is how a detector's precision is measured.
 *
 *  There is no AI text here (doc 31 section 8.2): this pane is where an
 *  operator decides the next few minutes, and a narrative would push the
 *  evidence off-screen. */

export function isDerivedAlarm(a: Pick<Alarm, 'detection' | 'source'>): boolean {
  return a.detection === 'derived' && a.source === 'analytics';
}

/** Lines shown before "Show all evidence". */
const FIRST_LINES = 4;

const BUCKET_MS: Record<string, number> = {
  raw: 60_000, '1m': 60_000, '5m': 300_000, '1h': 3600_000, '1d': 86400_000,
};

export function AnomalyEvidence({ alarm }: { alarm: Alarm }) {
  const health = useQuery({
    queryKey: ['anomaly-health'], queryFn: api.anomalyHealth, staleTime: 30_000,
  });
  // Every state: a cleared alarm's finding is resolved, and the API's default
  // (open, acknowledged, confirmed) would answer "not found" for it.
  const lookup = useQuery({
    queryKey: ['anomaly-findings', 'alarm', alarm.id],
    queryFn: () => api.anomalyFindings({ alarm_id: alarm.id, state: FINDING_STATES, limit: '1' }),
  });
  const findingId = lookup.data?.items[0]?.id;
  const detail = useQuery({
    queryKey: ['anomaly-finding', findingId],
    queryFn: () => api.anomalyFinding(findingId as string),
    enabled: Boolean(findingId),
  });

  useAnomalyLive((frame) => {
    const f = frameFinding(frame);
    if (!f || (f.id !== findingId && f.alarm_id !== alarm.id)) return [];
    return [['anomaly-findings', 'alarm', alarm.id], ['anomaly-finding', findingId ?? '']];
  });

  const loading = lookup.isLoading || (Boolean(findingId) && detail.isLoading);
  const error = lookup.error ?? detail.error;

  return (
    <section className="anom-ev" aria-labelledby={`anom-ev-${alarm.id}`}>
      <div className="anom-ev-head">
        <h4 id={`anom-ev-${alarm.id}`}>Evidence</h4>
        {detail.data && <ByLine f={detail.data} />}
      </div>

      {loading && (
        <div className="anom-skel" aria-busy="true" aria-label="Loading the evidence">
          <div className="asset-skeleton" style={{ width: '70%' }} />
          <div className="asset-skeleton" style={{ width: '90%' }} />
          <div className="asset-skeleton" style={{ width: '55%' }} />
        </div>
      )}

      {!loading && error && (
        <Notice tone="error">
          Could not load the evidence: {(error as Error).message}
        </Notice>
      )}

      {!loading && !error && lookup.data && !findingId && !health.isLoading && (
        <p className="anom-ev-cap">
          {health.data && !health.data.enabled
            ? 'Raised by analysis. Anomaly detection is off on this platform now, so its finding record is not served.'
            : 'Raised by analysis, but its finding record was not found — it may have been purged by retention.'}
        </p>
      )}

      {detail.data && <Body f={detail.data} alarm={alarm}
                            severityCap={health.data?.severity_cap}
                            l5Major={health.data?.l5_major_enabled ?? false} />}
    </section>
  );
}

function ByLine({ f }: { f: AnomalyFindingDetail }) {
  return (
    <span className="anom-ev-by">
      Raised by analysis ·
      <span className="anom-id">{f.detector_id}</span>
      {f.detector_name ?? f.detector.name} v{f.detector_version}{' '}
      <span className="anom-tag" title={LAYER_NAME[f.layer]}>{f.layer}</span>
    </span>
  );
}

function Body({ f, alarm, severityCap, l5Major }: {
  f: AnomalyFindingDetail; alarm: Alarm; severityCap?: string; l5Major: boolean;
}) {
  const unit = f.unit ?? null;
  // A difference of temperatures is kelvin, not degrees.
  const dUnit = unit === 'C' ? 'K' : unit;
  const ev = f.evidence ?? {};
  const observed = f.observed ?? ev.observed ?? null;
  const expected = f.expected ?? ev.expected ?? null;
  const limit = f.rule_limit ?? null;
  const upper = limit ? ev.upper != null || /upper/.test(limit.label) : false;
  const over = limit && observed != null ? observed - limit.value : null;
  const held = (new Date(f.last_seen).getTime() - new Date(f.first_seen).getTime()) / 1000;

  return (
    <>
      {alarm.severity === 'MAJOR' && l5Major ? (
        <p className="anom-ev-cap">
          Escalated to MAJOR by the cooling correlator: several racks are affected under one root.
        </p>
      ) : severityCap ? (
        <p className="anom-ev-cap">Derived alerts are capped at {severityCap}.</p>
      ) : null}

      <div className="anom-readout" role="group" aria-label="Limit and reading">
        {limit ? (
          <div>
            <span className="cap">Rule limit</span>
            <span className="val">{upper ? '≤' : '≥'} {num(limit.value, unit)}</span>
            <span className="sub">when it opened</span>
          </div>
        ) : expected != null ? (
          <div>
            <span className="cap">Expected</span>
            <span className="val">{num(expected, unit)}</span>
          </div>
        ) : null}
        <div className="is-observed">
          <span className="cap">Observed</span>
          <span className="val">{num(observed, unit)}</span>
          {over != null ? (
            <span className="sub">
              {signed(over, dUnit)} {upper ? (over > 0 ? 'over' : 'under') : (over < 0 ? 'below' : 'above')}
            </span>
          ) : expected != null && observed != null ? (
            <span className="sub">{signed(observed - expected, dUnit)} from expected</span>
          ) : null}
        </div>
        {limit && expected != null && (
          <div>
            <span className="cap">Expected</span>
            <span className="val">{num(expected, unit)}</span>
          </div>
        )}
        <div>
          <span className="cap">Held</span>
          <span className="val">{duration(held)}</span>
          <span className="sub">since {stamp(f.first_seen)}</span>
        </div>
      </div>

      <p className="anom-summary">{f.summary}</p>

      {f.metric_key && <EvidenceChart f={f} upper={upper} />}

      <Signals f={f} />

      <Verdict f={f} alarm={alarm} />
    </>
  );
}

/* ------------------------------------------------------------- the chart */

/** The primary series over the evidence window.
 *
 *  Drawn with the unmodified `TimeChart`. The moving rule limit is not drawn
 *  as a line: it is served by `/anomalies/expected` (`rule_limit` bands),
 *  which does not exist yet, and a flat line at the value it had when the
 *  finding opened would misstate a limit that follows the setpoint. The limit
 *  is printed under the chart instead, labelled with when it held. */
function EvidenceChart({ f, upper }: { f: AnomalyFindingDetail; upper: boolean }) {
  const [max, setMax] = useState(false);
  const metric = f.metric_key as string;
  const range = useMemo(() => {
    const end = f.resolved_at ? new Date(f.resolved_at).getTime() : Date.now();
    const winFrom = f.evidence?.window?.from ? new Date(f.evidence.window.from).getTime() : NaN;
    const first = new Date(f.first_seen).getTime();
    // Some lead-in before the condition began, and never under an hour.
    let start = Math.min(Number.isFinite(winFrom) ? winFrom : first, first - 30 * 60_000);
    start = Math.min(start, end - 3600_000);
    return { start: new Date(start).toISOString(), end: new Date(end).toISOString() };
    // Re-anchored only when the finding itself changes.
  }, [f.id, f.first_seen, f.resolved_at, f.evidence?.window?.from]);

  const q = useQuery({
    queryKey: ['anomaly-ev-history', f.device_id, metric, range.start, range.end],
    queryFn: () => api.history(f.device_id, [metric], range.start, range.end),
    staleTime: 60_000,
  });
  const series = (q.data?.series ?? []).filter(
    (s) => s.metric === metric && (!f.instance || s.instance === f.instance));
  const unit = series[0]?.unit ?? f.unit ?? '';
  const title = `${metricLabel(metric)}${f.instance ? ` · ${f.instance}` : ''}`;
  const limit = f.rule_limit;

  const chart = q.isLoading ? (
    <div className="asset-skeleton" style={{ height: 120 }} aria-label="Loading the chart" />
  ) : q.error ? (
    <p className="muted">Could not load the readings: {(q.error as Error).message}</p>
  ) : series.length && series.some((s) => s.points.length) ? (
    <TimeChart series={series} unit={unit} bucketMs={BUCKET_MS[q.data?.interval ?? ''] ?? 60_000} />
  ) : (
    <p className="muted">No readings of {metricLabel(metric)} were stored for this window.</p>
  );
  const foot = (
    <p className="anom-chart-foot">
      {limit
        ? `Rule limit ${upper ? '≤' : '≥'} ${num(limit.value, f.unit)} when the finding opened. `
        : ''}
      From {stamp(range.start)}{q.data ? ` · ${q.data.interval} buckets` : ''}.
    </p>
  );

  return (
    <div className="asset-panel">
      <h3>{title}</h3>
      <button type="button" className="asset-max" onClick={() => setMax(true)}
              aria-label={`Maximize ${title}`} title="Maximize">
        <MaxGlyph />
      </button>
      {chart}
      {foot}
      {max && (
        <MaxModal title={`${title} on ${f.device_name}`} onClose={() => setMax(false)}>
          {chart}
          {foot}
        </MaxModal>
      )}
    </div>
  );
}

/* ------------------------------------------------------------- evidence */

function signalText(s: FindingSignal, ownDevice: string): { what: string; fig: string } {
  const what = `${metricLabel(s.metric)}${s.instance ? ` · ${s.instance}` : ''}`
    + (s.device_id && s.device_id !== ownDevice ? ' (another device)' : '');
  if (s.kind === 'state') {
    const word = s.last === 1 ? 'on' : s.last === 0 ? 'off' : 'unknown';
    const since = s.since ? ` since ${stamp(s.since)}${s.since_is_transition ? '' : ' or earlier'}` : '';
    const starts = s.starts?.length ? ` · ${s.starts.length} start${s.starts.length === 1 ? '' : 's'} in the window` : '';
    return { what, fig: `${word}${since}${starts}` };
  }
  if (!s.n) return { what, fig: 'no clean readings in the window' };
  const u = METRICS[s.metric as MetricKey]?.unit ?? null;
  const parts = [`${num(s.min, u)}–${num(s.max, u)}`, `last ${num(s.last, u)}`];
  if (s.slope_per_h != null) parts.push(`${signed(s.slope_per_h, u)}/h`);
  parts.push(`n ${s.n}`);
  return { what, fig: parts.join(' · ') };
}

function Signals({ f }: { f: AnomalyFindingDetail }) {
  const [all, setAll] = useState(false);
  const [copied, setCopied] = useState(false);
  const signals = f.evidence?.signals ?? [];
  const masked = new Set((f.evidence?.masks ?? []).filter((m) => m.masked).map((m) => m.series));
  if (!signals.length) return null;
  const lines = signals.map((s) => {
    const t = signalText(s, f.device_id);
    const isMasked = masked.has(`${s.device_id}|${s.metric}|${s.instance}`);
    return { id: s.id, ...t, masked: isMasked };
  });
  const shown = all ? lines : lines.slice(0, FIRST_LINES);

  const copy = async () => {
    const head = `${f.detector_id} ${f.detector_name ?? ''} v${f.detector_version} on ${f.device_name}`;
    const text = [head, f.summary,
      ...lines.map((l) => `${l.id} ${l.what}: ${l.fig}${l.masked ? ' (masked as a bad reading)' : ''}`)]
      .join('\n');
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2500);
    } catch { /* clipboard refused: the list stays on screen */ }
  };

  return (
    <div>
      <ul className="anom-signals" aria-label="What the rule read">
        {shown.map((l) => (
          <li key={l.id}>
            <span className="anom-eid">{l.id}</span>
            <span>
              <span className="what">{l.what}</span>{' '}
              <span className="fig">{l.fig}{l.masked ? ' · masked as a bad reading' : ''}</span>
            </span>
          </li>
        ))}
      </ul>
      <div className="anom-actions" style={{ marginTop: 8 }}>
        {lines.length > FIRST_LINES && (
          <button type="button" className="anom-linkbtn" aria-expanded={all}
                  onClick={() => setAll((x) => !x)}>
            {all ? 'Show fewer' : `Show all evidence (${lines.length})`}
          </button>
        )}
        <button type="button" className="anom-linkbtn" onClick={copy}>
          {copied ? 'Copied' : 'Copy evidence'}
        </button>
        <span className="anom-live" aria-live="polite">{copied ? 'Evidence copied' : ''}</span>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------- verdict */

const PENDING: Record<FeedbackAction, string> = {
  acknowledge: 'Acknowledging…', confirm: 'Confirming…', dismiss: 'Dismissing…', reopen: 'Reopening…',
};
const DONE: Record<FeedbackAction, string> = {
  acknowledge: 'Acknowledged', confirm: 'Confirmed', dismiss: 'Dismissed', reopen: 'Reopened',
};
const VERB: Record<FeedbackAction, string> = {
  acknowledge: 'acknowledge', confirm: 'confirm', dismiss: 'dismiss', reopen: 'reopen',
};

function Verdict({ f, alarm }: { f: AnomalyFindingDetail; alarm: Alarm }) {
  const qc = useQueryClient();
  const canAct = atLeast('operator');
  const [dialog, setDialog] = useState<'confirm' | 'dismiss' | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const send = useMutation({
    mutationFn: (body: { action: FeedbackAction; reason?: DismissReason; note?: string }) =>
      api.findingFeedback(f.id, body),
    onSuccess: (_row, body) => {
      setProblem(null);
      setDialog(null);
      setMessage(body.action === 'reopen' && f.lane === 'alert'
        ? 'Reopened. The alarm stays acknowledged.' : DONE[body.action]);
    },
    onError: (e, body) => {
      if (e instanceof ApiError && e.status === 409 && e.detail?.error === 'illegal_transition') {
        const state = String(e.detail.state ?? 'changed');
        setProblem(`This finding changed since you opened it — it is now ${state}.`);
        setDialog(null);
      } else {
        setProblem(`Could not ${VERB[body.action]}: ${(e as Error).message}`);
      }
    },
    onSettled: () => {
      for (const k of ['anomaly-findings', 'anomaly-finding', 'anomaly-summary',
                       'device-findings', 'alarms', 'alarm']) {
        qc.invalidateQueries({ queryKey: [k] });
      }
    },
  });

  const st = f.state;
  const canJudge = st === 'open' || st === 'acknowledged';
  const canReopen = st === 'acknowledged' || st === 'confirmed' || st === 'dismissed';
  const last = f.feedback.length ? f.feedback[f.feedback.length - 1] : null;
  const reasonWord = (r?: string | null) => DISMISS_REASONS.find((x) => x.key === r)?.label;

  return (
    <div style={{ display: 'grid', gap: 8 }}>
      <p className="anom-state">
        Finding:{' '}
        <b className={st === 'dismissed' ? 'is-dismissed' : undefined}>{STATE_WORD[st]}</b>
        {st === 'resolved' && (
          <> · {relativeTime(f.resolved_at)}
            {f.resolution ? ` · ${RESOLUTION_WORD[f.resolution]}` : ''}
            {f.verdict ? ` · was ${f.verdict}` : ''}</>
        )}
        {' · '}{KIND_WORD[f.kind].toLowerCase()} {f.lane === 'alert' ? 'alert' : 'advisory'}
        {last && (
          <> · {DONE[last.action].toLowerCase()} by {last.by} {relativeTime(last.at)}
            {last.via === 'alarm' ? ' through the alarm' : ''}
            {last.reason ? ` — ${reasonWord(last.reason) ?? last.reason}` : ''}</>
        )}
      </p>
      {st === 'open' && f.lane === 'alert' && alarm.state === 'ACTIVE' && (
        <p className="anom-note" style={{ margin: 0 }}>
          Acknowledge the alarm and the finding follows within one sweep.
        </p>
      )}

      {canAct ? (
        (canJudge || canReopen) && (
          <div className="anom-actions">
            {canJudge && (
              <button type="button" disabled={send.isPending} onClick={() => setDialog('confirm')}>
                Confirm fault
              </button>
            )}
            {canJudge && (
              <button type="button" disabled={send.isPending} onClick={() => setDialog('dismiss')}>
                Dismiss
              </button>
            )}
            {canReopen && (
              <button type="button" disabled={send.isPending}
                      onClick={() => send.mutate({ action: 'reopen' })}>
                {send.isPending && send.variables?.action === 'reopen' ? PENDING.reopen : 'Reopen'}
              </button>
            )}
          </div>
        )
      ) : (
        <p className="anom-note" style={{ margin: 0 }}>
          Confirming or dismissing a finding needs an operator.
        </p>
      )}

      <span className="anom-live" aria-live="polite">{message ?? ''}</span>
      {message && !problem && <p className="anom-note" style={{ margin: 0 }}>{message}</p>}
      {problem && <Notice tone="error">{problem}</Notice>}

      {dialog === 'confirm' && (
        <ConfirmDialog f={f} pending={send.isPending}
                       error={send.isError ? problem : null}
                       onClose={() => setDialog(null)}
                       onSend={(note) => send.mutate({ action: 'confirm', note })} />
      )}
      {dialog === 'dismiss' && (
        <DismissDialog f={f} pending={send.isPending}
                       error={send.isError ? problem : null}
                       onClose={() => setDialog(null)}
                       onSend={(reason, note) => send.mutate({ action: 'dismiss', reason, note })} />
      )}
    </div>
  );
}

function NoteField({ value, onChange, id }: { value: string; onChange: (v: string) => void; id: string }) {
  return (
    <label className="anom-field" htmlFor={id}>
      <span>Note (optional)</span>
      <textarea id={id} className="anom-textarea" maxLength={1000} value={value}
                onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

function ConfirmDialog({ f, pending, error, onClose, onSend }: {
  f: AnomalyFindingDetail; pending: boolean; error: string | null;
  onClose: () => void; onSend: (note?: string) => void;
}) {
  const [note, setNote] = useState('');
  return (
    <Dialog title={`Confirm fault on ${f.device_name}?`} onClose={onClose}>
      <p className="anom-dialog-copy">
        Confirming counts this finding as a real fault in {f.detector_id}’s precision.
        {f.kind === 'process' ? ' Its window is also kept out of the next baseline fit.' : ''}
      </p>
      <NoteField id={`confirm-note-${f.id}`} value={note} onChange={setNote} />
      {error && <Notice tone="error">{error}</Notice>}
      <DialogActions>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" className="primary" disabled={pending}
                onClick={() => onSend(note.trim() || undefined)}>
          {pending ? PENDING.confirm : 'Confirm fault'}
        </button>
      </DialogActions>
    </Dialog>
  );
}

function DismissDialog({ f, pending, error, onClose, onSend }: {
  f: AnomalyFindingDetail; pending: boolean; error: string | null;
  onClose: () => void; onSend: (reason: DismissReason, note?: string) => void;
}) {
  const [reason, setReason] = useState<DismissReason | null>(null);
  const [note, setNote] = useState('');
  return (
    <Dialog title={`Dismiss ${f.detector_id} on ${f.device_name}?`} onClose={onClose}>
      <p className="anom-dialog-copy">
        Dismissing counts against {f.detector_id}’s precision. It does not mute the
        detector — to stop it on this device, add an override in Settings.
      </p>
      <p className="anom-dialog-copy muted">
        The finding stays dismissed until the condition clears; it will not re-raise meanwhile.
      </p>
      <fieldset className="anom-reasons" aria-describedby={`dismiss-req-${f.id}`}>
        <legend>Why is this not a fault?</legend>
        {DISMISS_REASONS.map((r) => (
          <label key={r.key}>
            <input type="radio" name={`dismiss-${f.id}`} value={r.key}
                   checked={reason === r.key} onChange={() => setReason(r.key)} />
            <span>{r.label}</span>
            <span className="hint">{r.hint}</span>
          </label>
        ))}
      </fieldset>
      {!reason && <p id={`dismiss-req-${f.id}`} className="anom-note">Choose a reason.</p>}
      <NoteField id={`dismiss-note-${f.id}`} value={note} onChange={setNote} />
      {error && <Notice tone="error">{error}</Notice>}
      <DialogActions>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" className="primary" disabled={!reason || pending}
                onClick={() => reason && onSend(reason, note.trim() || undefined)}>
          {pending ? PENDING.dismiss : 'Dismiss'}
        </button>
      </DialogActions>
    </Dialog>
  );
}
