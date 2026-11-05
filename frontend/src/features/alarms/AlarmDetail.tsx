import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  api, type Alarm, type AlarmContext, type AlarmHistoryEntry, type AlarmRelated, type Series,
} from '../../api/client';
import { CategoryGlyph } from '../../components/CategoryGlyph';
import { metaFor } from '../../components/alertMeta';
import { Notice } from '../../components/Notice';
import { StatusChip } from '../../components/StatusChip';
import { TimeChart, type ChartMark } from '../../components/TimeChart';
import { atLeast } from '../../api/client';
import { humanise, metricLabel, relativeTime } from '../../lib/format';
import { TicketChip } from './TicketChip';
import { AnomalyEvidence, isDerivedAlarm } from './AnomalyEvidence';
import { IncidentTree, useDeviceIncident } from './IncidentTree';

/* One condition, in full: what and how bad, where, why, what was measured,
   what has happened to it, and what to do. Opened over the home page's
   alarm panel, from a device, a rack, the 3D view, a ticket or an email.

   Reads the alarm again by id rather than trusting the row it was opened
   with: a list row is a slice, and the detail carries the rule, the note,
   the window and the fold that the list never needed. */

const BUCKET_MS: Record<string, number> = { raw: 15_000, '1m': 60_000, '5m': 300_000, '1h': 3_600_000, '1d': 86_400_000 };
const HOUR = 3_600_000;

const stamp = (iso?: string | null) => (iso ? new Date(iso).toLocaleString([], {
  day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—');
const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

/** The action, as a verb an operator reads: who did what. */
function historyLine(h: AlarmHistoryEntry): { what: string; by: string; more?: string } {
  const d = (h.detail ?? {}) as Record<string, unknown>;
  const by = h.actor === 'system' || h.actor === 'device' || h.actor === 'correlation' || !h.actor
    ? (h.actor === 'device' ? 'the device' : h.actor === 'correlation' ? 'correlation' : 'the platform')
    : h.actor;
  switch (h.action) {
    case 'raised': return { what: `Raised at ${(h.severity ?? '').toLowerCase()}`, by,
      more: d.value != null ? `reading ${d.value}${d.threshold != null ? `, limit ${d.threshold}` : ''}` : undefined };
    case 'escalated': return { what: `Escalated to ${(h.severity ?? '').toLowerCase()}`, by };
    case 'deescalated': return { what: `Lowered to ${(h.severity ?? '').toLowerCase()}`, by };
    case 'suppressed':
    case 'correlated': return { what: 'Attributed to its cause', by,
      more: d.root_device ? `${d.root_device}${d.tier ? ` (${d.tier})` : ''}` : undefined };
    case 'released': return { what: 'Released: its cause cleared', by, more: d.reason ? String(d.reason) : undefined };
    case 'acknowledged': return { what: 'Acknowledged', by, more: d.note ? `“${d.note}”` : undefined };
    case 'cleared': return { what: 'Cleared', by, more: d.reason ? String(d.reason) : undefined };
    case 'shelved': return { what: 'Shelved by planned work', by, more: d.window ? String(d.window) : undefined };
    default: return { what: humanise(h.action), by };
  }
}

function Related({ a, onOpen }: { a: AlarmRelated; onOpen: (id: string) => void }) {
  return (
    <li>
      <StatusChip status={a.severity} />
      <button type="button" className="link-button" onClick={() => onOpen(a.id)}>
        {a.device_name ?? 'platform'}
      </button>
      <span className="muted">
        {humanise(a.alarm_type)}{a.instance ? ` · ${a.instance}` : ''}
        {a.room_name ? ` · ${a.room_name}` : ''}
        {a.state === 'CLEARED' ? ' · cleared' : ` · ${relativeTime(a.first_seen)}`}
      </span>
    </li>
  );
}

/** The measured series around the alarm, with the limit it crossed. */
function Measured({ alarm }: { alarm: Alarm }) {
  const from = Date.parse(alarm.first_seen) - HOUR;
  const to = Math.min(Date.now(), (alarm.cleared_at ? Date.parse(alarm.cleared_at) : Date.now()) + HOUR / 2);
  const q = useQuery({
    queryKey: ['alarm-series', alarm.id, alarm.metric_key, to],
    queryFn: () => api.history(alarm.device_id, [alarm.metric_key!],
                               new Date(from).toISOString(), new Date(to).toISOString()),
    enabled: Boolean(alarm.device_id && alarm.metric_key),
    retry: false,
    staleTime: 60_000,
  });
  const series = useMemo(() => {
    const all = q.data?.series ?? [];
    const own = all.filter((s) => s.instance === (alarm.instance ?? '') || all.length === 1);
    return own.length ? own : all.slice(0, 1);
  }, [q.data, alarm.instance]);
  const unit = series[0]?.unit ?? '';
  const reference: Series[] | undefined = alarm.threshold != null && series.length
    ? [{ metric: 'limit', instance: '', unit, points: [[from, alarm.threshold], [to, alarm.threshold]] }]
    : undefined;
  const marks: ChartMark[] = [{
    start: Date.parse(alarm.first_seen),
    end: alarm.cleared_at ? Date.parse(alarm.cleared_at) : null,
    severity: alarm.severity, label: alarm.message || humanise(alarm.alarm_type),
  }];
  if (!alarm.metric_key) return null;
  return (
    <section className="ad-section">
      <h4>Measured</h4>
      <div className="ad-reading">
        <b className="ad-value">
          {alarm.trigger_value != null ? `${alarm.trigger_value}${unit ? ` ${unit}` : ''}` : '—'}
        </b>
        {alarm.threshold != null && (
          <span className="muted">against a limit of {alarm.threshold}{unit ? ` ${unit}` : ''}</span>
        )}
        <span className="muted">
          · {metricLabel(alarm.metric_key)}{alarm.instance ? ` · ${alarm.instance}` : ''}
          {alarm.trigger_va != null ? ` · ${(alarm.trigger_va / 1000).toFixed(2)} kVA` : ''}
        </span>
      </div>
      {q.isLoading && <p className="ad-note">Reading the last hours…</p>}
      {q.isError && <p className="ad-note">The series could not be read.</p>}
      {q.data && series.length > 0 && (
        <div className="ad-chart">
          <TimeChart series={series} unit={unit} reference={reference} marks={marks}
                     bucketMs={BUCKET_MS[q.data.interval] ?? 60_000}
                     window={[from, to]} />
          <p className="ad-note">
            One hour before it was raised{alarm.cleared_at ? ' to half an hour after it cleared' : ' to now'};
            the thin line is the limit, the bar under the axis is this alarm.
          </p>
        </div>
      )}
      {q.data && series.length === 0 && <p className="ad-note">No samples stored for this window.</p>}
    </section>
  );
}

export function AlarmDetailModal({ alarm: given, onClose, onOpenAlarm }: {
  alarm: Alarm; onClose: () => void;
  /** Open another alarm in this place - its cause, or one it explains. */
  onOpenAlarm?: (id: string) => void;
}) {
  const qc = useQueryClient();
  const full = useQuery({
    queryKey: ['alarm', given.id],
    queryFn: () => api.alarm(given.id),
    placeholderData: given,
  });
  const alarm = full.data ?? given;
  const ctx = useQuery<AlarmContext>({
    queryKey: ['alarm-context', alarm.id, alarm.state, alarm.last_seen],
    queryFn: () => api.alarmContext(alarm.id),
    retry: false,
  });
  // The cooling incident this alarm roots, if any: a cause's Cause section
  // lists what it explains, and the incident is the fuller account of that.
  const { belongs: inIncident } = useDeviceIncident(alarm.device_id, alarm.id);
  const incident = inIncident === true;
  const incidentKnown = inIncident !== undefined;
  const [note, setNote] = useState('');
  const [noting, setNoting] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      const t = e.target as Element | null;
      if (t?.closest?.('.asset-dialog') || document.querySelector('.max-modal')) return;
      e.stopPropagation(); onClose();
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [onClose]);

  const refresh = () => {
    for (const key of [['alarms'], ['alarm'], ['alarm-context'], ['alarm-summary'],
                       ['room-conditions'], ['estate-alarms'], ['dashboard'], ['sites-overview']]) {
      qc.invalidateQueries({ queryKey: key });
    }
  };
  const ack = useMutation({
    mutationFn: () => api.acknowledgeAlarm(alarm.id, note.trim() || undefined),
    onSuccess: () => { setNoting(false); setNote(''); refresh(); },
  });
  const clear = useMutation({
    mutationFn: () => api.clearAlarm(alarm.id),
    onSuccess: () => { refresh(); onClose(); },
  });

  const meta = metaFor(alarm.category ?? '');
  const sev = alarm.severity.toLowerCase();
  const open = alarm.state !== 'CLEARED';
  const where = [alarm.datacenter_code, alarm.room_name, alarm.rack_name].filter(Boolean);
  const openOther = (id: string) => { if (onOpenAlarm) onOpenAlarm(id); };
  const history = ctx.data?.history ?? [];
  const emails = ctx.data?.emails ?? [];
  const timeline = useMemo(() => {
    const rows = history.map((h) => ({ ts: h.ts, ...historyLine(h) }));
    for (const e of emails) {
      const kind = e.kind === 'alarm_cleared' ? 'cleared' : e.kind === 'alarm_escalated' ? 'escalated' : 'raised';
      const to = Array.isArray(e.recipients) ? e.recipients.length : 0;
      rows.push({
        ts: e.sent_at ?? e.created_at,
        what: e.status === 'sent' ? `Email sent: ${kind}` : `Email ${e.status}: ${kind}`,
        by: e.rule_name,
        more: `${to ? `${to} recipient${to === 1 ? '' : 's'}` : ''}${e.note ? `${to ? ' · ' : ''}${e.note}` : ''}` || undefined,
      });
    }
    return rows.sort((a, b) => b.ts.localeCompare(a.ts));
  }, [history, emails]);

  return (
    <div className="modal-scrim over-sheet" role="dialog" aria-modal="true"
         aria-label={`${humanise(alarm.alarm_type)} on ${alarm.device_name}`}
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className={`modal alarm-modal ad sev-${sev}`}>
        <div className="modal-head ad-head">
          <div className="ad-head-text">
            <div className="ad-tags">
              <StatusChip status={alarm.severity} />
              <span className={`ad-state ${alarm.state.toLowerCase()}`}>{alarm.state.toLowerCase()}</span>
              <span className="ad-class">{alarm.response_class === 'alert' ? 'alert — informational' : 'alarm — needs a response'}</span>
              {alarm.category && (
                <span className={`ad-cat cat-${meta.tone}`}><CategoryGlyph kind={meta.glyph} size={13} />{humanise(alarm.category)}</span>
              )}
              {alarm.is_symptom && <span className="ad-folded">folded under a cause</span>}
              {alarm.shelved_by_window && <span className="ad-folded">shelved by planned work</span>}
            </div>
            <h3>{humanise(alarm.alarm_type)}{alarm.instance && alarm.instance.length < 24 ? <span className="muted"> · {alarm.instance}</span> : null}</h3>
            <p className="ad-where">
              {alarm.device_id
                ? <Link to={`/devices/${alarm.device_id}`}><b>{alarm.device_name}</b></Link>
                : <b>The platform</b>}
              {alarm.device_type ? <span className="muted"> {humanise(alarm.device_type)}</span> : null}
              {where.length > 0 && <span className="muted"> · {where.join(' › ')}</span>}
              {alarm.instance_feeds && <span className="muted"> · feeds {alarm.instance_feeds}</span>}
            </p>
            <p className="ad-since">
              {open ? `Open ${relativeTime(alarm.first_seen).replace(/ ago$/, '')}` : `Cleared ${relativeTime(alarm.cleared_at ?? alarm.last_seen)}`}
              {' · '}first seen {stamp(alarm.first_seen)} · last seen {stamp(alarm.last_seen)}
              {alarm.occurrence_count > 1 ? ` · seen ${alarm.occurrence_count} times` : ''}
            </p>
          </div>
          <button className="close" onClick={onClose} aria-label="Close">×</button>
        </div>

        <div className="modal-body ad-body">
          {alarm.message && <blockquote className="ad-message">{alarm.message}</blockquote>}

          <div className="ad-actions">
            {alarm.state === 'ACTIVE' && atLeast('operator') && !noting && (
              <button className="primary" disabled={ack.isPending} onClick={() => setNoting(true)}>Acknowledge</button>
            )}
            {open && atLeast('operator') && (
              <button disabled={clear.isPending} onClick={() => { if (window.confirm('Clear this alarm by hand? If the condition is still true it will be raised again.')) clear.mutate(); }}>
                {clear.isPending ? 'Clearing…' : 'Clear by hand'}
              </button>
            )}
            <TicketChip alarmId={alarm.id} />
            {alarm.device_id && <Link className="ad-link" to={`/devices/${alarm.device_id}`}>Device page</Link>}
            {alarm.room_name && alarm.device_id && (
              <Link className="ad-link" to={`/floorplan?device=${alarm.device_id}`}>In 3D</Link>
            )}
          </div>
          {noting && (
            <form className="ad-ack" onSubmit={(e) => { e.preventDefault(); ack.mutate(); }}>
              <label htmlFor="ad-note">Note for the record (optional)</label>
              <input id="ad-note" value={note} onChange={(e) => setNote(e.target.value)}
                     placeholder="What you have done, or what you are waiting for" autoFocus />
              <div className="ad-ack-row">
                <button type="submit" className="primary" disabled={ack.isPending}>
                  {ack.isPending ? 'Acknowledging…' : 'Acknowledge'}
                </button>
                <button type="button" onClick={() => { setNoting(false); setNote(''); }}>Cancel</button>
              </div>
            </form>
          )}
          {(ack.error || clear.error) && (
            <Notice tone="error">{String(((ack.error ?? clear.error) as Error).message)}</Notice>
          )}
          {alarm.acknowledged_at && (
            <p className="ad-ackd">
              Acknowledged by <b>{alarm.acknowledged_by ?? 'an operator'}</b> {relativeTime(alarm.acknowledged_at)}
              {alarm.ack_note ? <> — “{alarm.ack_note}”</> : null}
            </p>
          )}

          <Measured alarm={alarm} />

          <section className="ad-section">
            <h4>Cause</h4>
            {/* A consequence names its cause and stops there. How the
                correlator keeps or folds it is not the operator's concern. */}
            {ctx.data?.root && (
              <p className="ad-cause">
                Caused by{' '}
                <button type="button" className="link-button" onClick={() => openOther(ctx.data!.root!.id)}>
                  {ctx.data.root.device_name ?? 'platform'}: {humanise(ctx.data.root.alarm_type)}
                </button>
                {' '}<StatusChip status={ctx.data.root.severity} />
                <span className="muted"> · {ctx.data.root.state === 'CLEARED' ? `cleared ${relativeTime(ctx.data.root.cleared_at ?? ctx.data.root.first_seen)}` : `open since ${relativeTime(ctx.data.root.first_seen)}`}</span>
              </p>
            )}
            {alarm.shelved_by_window && (
              <p className="ad-cause">
                Shelved by planned work:{' '}
                <Link to={`/assets/maintenance/${alarm.shelved_by_window}`}>{alarm.shelved_window_title ?? 'a maintenance window'}</Link>
                {alarm.shelved_reason ? <span className="muted"> · {alarm.shelved_reason}</span> : null}
              </p>
            )}
            {/* A cause lists what it explains: the incident when it roots
                one, else the alarms tied directly to it. */}
            {!ctx.data?.root && incident && (
              <IncidentTree deviceId={alarm.device_id} alarmId={alarm.id} heading={false} />
            )}
            {!ctx.data?.root && incidentKnown && !incident && (ctx.data?.symptom_count ?? 0) > 0 && (
              <>
                <p className="ad-cause">
                  Explains <b>{ctx.data!.symptom_count}</b> other {ctx.data!.symptom_count === 1 ? 'alarm' : 'alarms'}:
                </p>
                <ul className="ad-related">
                  {ctx.data!.symptoms.map((s) => <Related key={s.id} a={s} onOpen={openOther} />)}
                </ul>
                {ctx.data!.symptom_count > ctx.data!.symptoms.length && (
                  <p className="ad-note">and {ctx.data!.symptom_count - ctx.data!.symptoms.length} more.</p>
                )}
              </>
            )}
            {ctx.data && !ctx.data.root && incidentKnown && !incident && !ctx.data.symptom_count && !alarm.shelved_by_window && !alarm.cause_tier && (
              <p className="ad-note">Stands on its own: nothing upstream explains it, and it explains nothing else.</p>
            )}
          </section>

          {isDerivedAlarm(alarm) && (
            <section className="ad-section">
              <h4>Evidence</h4>
              <AnomalyEvidence alarm={alarm} />
            </section>
          )}

          <section className="ad-section">
            <h4>Timeline</h4>
            {ctx.isLoading && <p className="ad-note">Reading the record…</p>}
            {ctx.isError && <p className="ad-note">The record could not be read.</p>}
            {timeline.length > 0 && (
              <ol className="ad-timeline">
                {timeline.map((t, i) => (
                  <li key={i}>
                    <time dateTime={t.ts} title={stamp(t.ts)}>{clock(t.ts)}</time>
                    <span className="ad-tl-what">{t.what}</span>
                    <span className="ad-tl-by">{t.by}</span>
                    {t.more && <span className="ad-tl-more">{t.more}</span>}
                  </li>
                ))}
              </ol>
            )}
          </section>

          <section className="ad-section ad-record">
            <h4>Record</h4>
            <dl className="kv">
              <dt>Raised by</dt>
              <dd>
                {alarm.rule_name
                  ? <>rule <Link to="/settings/alarm-rules">{alarm.rule_name}</Link></>
                  : alarm.source === 'snmp_trap' ? 'a trap from the device'
                  : alarm.source === 'correlation' ? 'the cooling correlator'
                  : humanise(alarm.source)}
                {alarm.detection ? <span className="muted"> · {alarm.detection} detection</span> : null}
              </dd>
              <dt>Source</dt><dd className="mono">{alarm.source}</dd>
              {alarm.cleared_at && (
                <>
                  <dt>Cleared</dt>
                  <dd>{stamp(alarm.cleared_at)}{alarm.cleared_by ? <span className="muted"> by {alarm.cleared_by}</span> : null}</dd>
                </>
              )}
              <dt>Alarm id</dt>
              <dd className="mono ad-id">
                {alarm.id}
                <button type="button" className="link-button" onClick={() => navigator.clipboard?.writeText(`${window.location.origin}/alarms/${alarm.id}`)}>copy link</button>
              </dd>
            </dl>
          </section>
        </div>
      </div>
    </div>
  );
}
