import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { api, type Alarm } from '../../api/client';
import { StatusChip } from '../../components/StatusChip';
import { humanise, relativeTime } from '../../lib/format';
import { useInvalidateOn, useTopics } from '../../ws/useSocket';

const ALARM_EVENTS = ['alarm_created', 'alarm_updated', 'alarm_cleared'];

/** Which lifecycle slice the list shows. The API keys on alarm STATE;
 *  "open" is its default (active + acknowledged), "cleared" is the history
 *  a quiet estate still has, "all" is both. */
const VIEWS: Record<string, string[] | undefined> = {
  open: undefined,
  cleared: ['CLEARED'],
  all: ['ACTIVE', 'ACKNOWLEDGED', 'CLEARED'],
};

export function AlarmList() {
  const [severity, setSeverity] = useState('');
  const [includeSymptoms, setIncludeSymptoms] = useState(false);
  const [selected, setSelected] = useState<Alarm | null>(null);
  // In the URL, so "the history" is a link somebody pastes - and so the
  // Home page can open it directly on a quiet day.
  const [params, setParams] = useSearchParams();
  const rawView = params.get('view') ?? 'open';
  const view = rawView in VIEWS ? rawView : 'open';
  const qc = useQueryClient();

  useTopics(['alarms']);
  useInvalidateOn(ALARM_EVENTS, [['alarms'], ['alarm-summary'], ['dashboard']]);

  const { data, error, isLoading } = useQuery({
    queryKey: ['alarms', severity, includeSymptoms, view,
               params.get('room') ?? ''],
    queryFn: () => api.alarms({
      severity: severity || undefined,
      include_symptoms: includeSymptoms ? 'true' : undefined,
      state: VIEWS[view],
      room: params.get('room') ?? undefined,
      limit: '200',
    }),
  });

  function setView(next: string) {
    const q = new URLSearchParams(params);
    if (next === 'open') q.delete('view');
    else q.set('view', next);
    setParams(q, { replace: true });
  }

  const summary = useQuery({ queryKey: ['alarm-summary'], queryFn: api.alarmSummary });

  const ack = useMutation({
    mutationFn: (id: string) => api.acknowledgeAlarm(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['alarms'] }),
  });
  const clear = useMutation({
    mutationFn: (id: string) => api.clearAlarm(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['alarms'] });
      setSelected(null);
    },
  });

  const s = summary.data;

  return (
    <>
      <h2>Alarms</h2>
      <p className="subtitle">
        Root causes only by default — one failure with twenty downstream
        symptoms should read as one incident, not twenty.
      </p>

      {s && (
        <div className="tiles" style={{ marginBottom: 16 }}>
          <div className="tile">
            <div className="label">Active</div>
            <div className="value">{s.active}</div>
            <div className="detail">{s.acknowledged} acknowledged</div>
          </div>
          <div className="tile">
            <div className="label">Critical</div>
            <div className="value" style={{ color: 'var(--critical)' }}>{s.critical}</div>
          </div>
          <div className="tile">
            <div className="label">Major</div>
            <div className="value" style={{ color: 'var(--major)' }}>{s.major}</div>
          </div>
          <div className="tile">
            <div className="label">Warning</div>
            <div className="value" style={{ color: 'var(--warn)' }}>{s.warning}</div>
          </div>
          <div className="tile">
            <div className="label">Suppressed</div>
            <div className="value">{s.suppressed_symptoms}</div>
            <div className="detail">symptoms of a root cause</div>
          </div>
        </div>
      )}

      <div className="toolbar">
        <select value={view} onChange={(e) => setView(e.target.value)}
                aria-label="Alarm state">
          <option value="open">Open</option>
          <option value="cleared">Cleared — history</option>
          <option value="all">All</option>
        </select>
        <select value={severity} onChange={(e) => setSeverity(e.target.value)}>
          <option value="">Any severity</option>
          <option value="CRITICAL">Critical</option>
          <option value="MAJOR">Major</option>
          <option value="MINOR">Minor</option>
          <option value="WARNING">Warning</option>
        </select>
        <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <input type="checkbox" checked={includeSymptoms}
                 onChange={(e) => setIncludeSymptoms(e.target.checked)} />
          <span className="muted">show suppressed symptoms</span>
        </label>
      </div>

      {isLoading && <p className="muted">Loading…</p>}
      {error && <div className="banner">Failed to load: {String(error)}</div>}

      {data && (
        <table>
          <thead>
            <tr>
              <th>Severity</th><th>Device</th><th>Alarm</th><th>Message</th>
              <th>Location</th><th className="num">Count</th><th>Last seen</th><th></th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((a) => (
              <tr key={a.id} onClick={() => setSelected(a)} style={{ cursor: 'pointer' }}>
                <td><StatusChip status={a.severity} /></td>
                <td><Link to={`/devices/${a.device_id}`} onClick={(e) => e.stopPropagation()}>
                  {a.device_name}
                </Link></td>
                <td>{humanise(a.alarm_type)}{a.instance ? <span className="muted"> · {a.instance}</span> : null}</td>
                <td className="muted">{a.message}</td>
                <td className="muted">
                  {[a.datacenter_code, a.room_name, a.rack_name].filter(Boolean).join(' · ') || '—'}
                </td>
                <td className="num">{a.occurrence_count}</td>
                <td className="muted">{relativeTime(a.last_seen)}</td>
                <td>
                  {a.state === 'ACTIVE' ? (
                    <button onClick={(e) => { e.stopPropagation(); ack.mutate(a.id); }}>
                      Ack
                    </button>
                  ) : (
                    <span className="muted">{a.state.toLowerCase()}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {data && data.items.length === 0 && (
        <p className="muted">No alarms match. Quiet is the correct default.</p>
      )}

      {selected && (
        <section className="stack" style={{ marginTop: 24 }}>
          <h3>{humanise(selected.alarm_type)} on {selected.device_name}</h3>
          <dl className="kv">
            <dt>Severity</dt><dd><StatusChip status={selected.severity} /></dd>
            <dt>State</dt><dd>{selected.state}</dd>
            <dt>Message</dt><dd>{selected.message}</dd>
            <dt>Source</dt><dd className="mono">{selected.source}</dd>
            <dt>Metric</dt>
            <dd className="mono">
              {selected.metric_key
                ? `${selected.metric_key} = ${selected.trigger_value} (limit ${selected.threshold})`
                : '—'}
            </dd>
            <dt>First seen</dt><dd>{relativeTime(selected.first_seen)}</dd>
            <dt>Last seen</dt><dd>{relativeTime(selected.last_seen)}</dd>
            <dt>Occurrences</dt><dd>{selected.occurrence_count}</dd>
          </dl>
          <div className="toolbar">
            <button onClick={() => clear.mutate(selected.id)}>Clear manually</button>
            <button onClick={() => setSelected(null)}>Close</button>
          </div>
        </section>
      )}
    </>
  );
}
