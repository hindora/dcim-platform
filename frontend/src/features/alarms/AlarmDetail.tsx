import { useEffect } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type Alarm } from '../../api/client';
import { StatusChip } from '../../components/StatusChip';
import { humanise, relativeTime } from '../../lib/format';
import { TicketChip } from './TicketChip';

/** One condition, in full.
 *
 *  Defined once and used twice - the alarm list's detail pane and the modal
 *  the alarm panel opens - because two copies of "what a DCIM says about an
 *  alarm" drift, and the one that drifts is always the one nobody is looking
 *  at while they edit the other.
 */
export function AlarmDetailBody({ alarm }: { alarm: Alarm }) {
  return (
    <dl className="kv">
      <dt>Severity</dt><dd><StatusChip status={alarm.severity} /></dd>
      <dt>State</dt><dd>{alarm.state}</dd>
      <dt>Class</dt>
      <dd>
        {alarm.response_class === 'alert'
          ? 'alert — informational, belongs to whoever schedules the work'
          : 'alarm — expects a response'}
      </dd>
      <dt>Message</dt><dd>{alarm.message}</dd>
      <dt>Device</dt>
      <dd>
        {alarm.device_id
          ? <Link to={`/devices/${alarm.device_id}`}>{alarm.device_name}</Link>
          : <span className="muted">platform</span>}
      </dd>
      <dt>Location</dt>
      <dd className="muted">
        {[alarm.datacenter_code, alarm.room_name, alarm.rack_name]
          .filter(Boolean).join(' · ') || '—'}
      </dd>
      <dt>Source</dt><dd className="mono">{alarm.source}</dd>
      <dt>Metric</dt>
      <dd className="mono">
        {alarm.metric_key
          ? `${alarm.metric_key} = ${alarm.trigger_value} (limit ${alarm.threshold})`
          : '—'}
      </dd>
      {alarm.instance && (
        <>
          <dt>Instance</dt><dd className="mono">{alarm.instance}</dd>
        </>
      )}
      <dt>First seen</dt><dd>{relativeTime(alarm.first_seen)}</dd>
      <dt>Last seen</dt><dd>{relativeTime(alarm.last_seen)}</dd>
      <dt>Occurrences</dt><dd>{alarm.occurrence_count}</dd>
      <dt>Ticket</dt><dd><TicketChip alarmId={alarm.id} /></dd>
    </dl>
  );
}

/** The same detail, over the alarm panel.
 *
 *  Its own scrim rather than the shared `Modal`, for one reason that is
 *  invisible until it bites: `.modal-scrim` sits at z-index 50 and the
 *  panel's `.sheet-scrim` at 60, so the shared modal opened from the panel
 *  renders BEHIND it and reads as a click that did nothing.
 */
export function AlarmDetailModal({ alarm, onClose }: {
  alarm: Alarm; onClose: () => void;
}) {
  const qc = useQueryClient();

  // Escape closes this, not the panel underneath. The panel already declines
  // to take an Escape that a maximized chart owns; this is the same rule for
  // the same reason.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.stopPropagation(); onClose(); }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [onClose]);

  const refresh = () => {
    // Every surface that counts this alarm, because the modal is opened from
    // a panel whose totals are the reason somebody is looking at it.
    for (const key of [['alarms'], ['alarm'], ['alarm-summary'],
                       ['room-conditions'], ['estate-alarms'], ['dashboard']]) {
      qc.invalidateQueries({ queryKey: key });
    }
  };

  const ack = useMutation({
    mutationFn: () => api.acknowledgeAlarm(alarm.id),
    onSuccess: refresh,
  });
  const clear = useMutation({
    mutationFn: () => api.clearAlarm(alarm.id),
    onSuccess: () => { refresh(); onClose(); },
  });

  return (
    <div className="modal-scrim over-sheet" role="dialog" aria-modal="true"
         aria-label={`${humanise(alarm.alarm_type)} on ${alarm.device_name}`}
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal alarm-modal">
        <div className="modal-head">
          <div>
            <h3>
              {humanise(alarm.alarm_type)}
              <span className="muted"> on {alarm.device_name}</span>
            </h3>
          </div>
          <button className="close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="modal-body">
          <AlarmDetailBody alarm={alarm} />
          <div className="toolbar">
            {alarm.state === 'ACTIVE' && (
              <button disabled={ack.isPending} onClick={() => ack.mutate()}>
                {ack.isPending ? 'Acknowledging…' : 'Acknowledge'}
              </button>
            )}
            {alarm.state !== 'CLEARED' && (
              <button disabled={clear.isPending} onClick={() => clear.mutate()}>
                {clear.isPending ? 'Clearing…' : 'Clear manually'}
              </button>
            )}
            <button onClick={onClose}>Close</button>
          </div>
          {(ack.error || clear.error) && (
            <div className="banner">
              {String(((ack.error ?? clear.error) as Error).message)}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
