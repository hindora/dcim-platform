import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  ApiError, api, atLeast,
  type AlarmRule, type AlarmRuleNew, type AlarmRulePatch,
} from '../../api/client';
import { usePaged } from '../../components/Pagination';
import { humanise, metricLabel, relativeTime } from '../../lib/format';
import { METRICS, type MetricKey } from '../../lib/metrics.gen';

/** The estate's alarm policy: every rule, what it watches, when it raises and
 *  when it lets go.
 *
 *  Editing a rule here moves the alarm for EVERY device it applies to, so it
 *  is an admin's action; one device that needs its own value gets an override
 *  from its own page, which an operator may set. A rule that watches a state
 *  (a leak input, a breaker bit) has no number to move, only a severity, a
 *  dwell and its wording. The built-in rules cannot be deleted, only turned
 *  off; rules you write can. */

const SEVERITIES = ['INFO', 'WARNING', 'MINOR', 'MAJOR', 'CRITICAL'];
const sevClass = (s: string) =>
  s === 'CRITICAL' ? 'critical' : s === 'MAJOR' ? 'major' : s === 'INFO' ? '' : 'warn';

const unitOf = (key: string | null) =>
  (key && METRICS[key as MetricKey]?.unit) || '';

/** "CPU Utilization > 80 %": the condition as a sentence an operator reads. */
function condition(r: AlarmRule): string {
  if (r.metric_kind === 'boolean' || r.operator == null) {
    return `${humanise(r.metric_key ?? r.alarm_type)} asserted`;
  }
  const u = unitOf(r.metric_key);
  return `${metricLabel(r.metric_key ?? '')} ${r.operator} ${r.threshold}${u ? ` ${u}` : ''}`;
}

type Scope = 'all' | 'threshold' | 'state' | 'mine' | 'overridden';

export function AlarmRules() {
  const qc = useQueryClient();
  const admin = atLeast('admin');
  const q = useQuery({ queryKey: ['alarm-rules'], queryFn: api.alarmRules, staleTime: 30_000 });
  const [text, setText] = useState('');
  const [scope, setScope] = useState<Scope>('all');
  const [editing, setEditing] = useState<AlarmRule | null>(null);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const all = useMemo(() => q.data?.items ?? [], [q.data]);
  const rows = useMemo(() => {
    const t = text.trim().toLowerCase();
    return all.filter((r) => {
      if (scope === 'threshold' && r.metric_kind !== 'numeric') return false;
      if (scope === 'state' && r.metric_kind === 'numeric') return false;
      if (scope === 'mine' && !r.custom && !r.edited_by) return false;
      if (scope === 'overridden' && !r.overrides) return false;
      return !t || `${r.name} ${r.alarm_type} ${r.metric_key ?? ''} ${r.device_types.join(' ')}`
        .toLowerCase().includes(t);
    });
  }, [all, text, scope]);
  const paged = usePaged(rows, { noun: 'rules', always: true });

  const refresh = () => qc.invalidateQueries({ queryKey: ['alarm-rules'] });
  const toggle = useMutation({
    mutationFn: (r: AlarmRule) => api.setAlarmRuleEnabled(r.id, !r.enabled),
    onSuccess: refresh,
    onError: (e) => setError(String((e as Error).message)),
  });

  return (
    <>
      <h2>Alarm rules</h2>
      <p className="subtitle">
        When each alarm raises and when it lets go. A change here applies to every device the
        rule covers{admin ? '' : '; an admin makes it'}. One device that needs its own value
        gets an override on its own page.
      </p>

      {error && <div className="banner" role="alert">{error}</div>}

      <div className="toolbar" style={{ display: 'flex', gap: 10, margin: '12px 0', flexWrap: 'wrap' }}>
        <input value={text} onChange={(e) => setText(e.target.value)} aria-label="Search rules"
               placeholder="Search by name, metric or device type…" style={{ minWidth: 280 }} />
        <select value={scope} onChange={(e) => setScope(e.target.value as Scope)}
                aria-label="Which rules">
          <option value="all">All rules</option>
          <option value="threshold">Thresholds</option>
          <option value="state">States</option>
          <option value="mine">Edited or written by us</option>
          <option value="overridden">With device overrides</option>
        </select>
        <span style={{ flex: 1 }} />
        {admin && <button className="primary" onClick={() => setAdding(true)}>New rule</button>}
      </div>

      {q.isLoading && <p className="muted">Loading…</p>}
      {q.isError && <div className="banner">The rules could not be read.</div>}

      <div className="asset-scroll">
        <table>
          <thead>
            <tr>
              <th>Rule</th><th>Raises when</th><th>Clears at</th><th>Held for</th>
              <th>Severity</th><th>Applies to</th><th>Overrides</th><th>On</th>
              {admin && <th />}
            </tr>
          </thead>
          <tbody>
            {paged.rows.map((r) => (
              <tr key={r.id} className={r.enabled ? undefined : 'muted'}>
                <td>
                  <b>{r.name}</b>
                  {r.custom && <small className="lock-chip">ours</small>}
                  <div className="muted small">
                    {r.edited_by ? `edited by ${r.edited_by} ${relativeTime(r.edited_at)}` : r.alarm_type}
                  </div>
                </td>
                <td>{condition(r)}</td>
                <td className="muted">
                  {r.metric_kind === 'numeric' && r.clear_threshold != null
                    ? `${r.clear_threshold}${unitOf(r.metric_key) ? ` ${unitOf(r.metric_key)}` : ''}` : '–'}
                </td>
                <td className="muted">
                  {r.dwell_samples} sample{r.dwell_samples === 1 ? '' : 's'}
                  {r.dwell_seconds ? ` · ${r.dwell_seconds}s` : ''}
                </td>
                <td><span className={`chip ${sevClass(r.severity)}`}>{r.severity.toLowerCase()}</span></td>
                <td className="muted">{r.device_types.length ? r.device_types.map(humanise).join(', ') : 'every type'}</td>
                <td className="muted">{r.overrides || '–'}</td>
                <td>
                  <input type="checkbox" checked={r.enabled} disabled={!admin || toggle.isPending}
                         aria-label={`${r.name} is ${r.enabled ? 'on' : 'off'}`}
                         onChange={() => toggle.mutate(r)} />
                </td>
                {admin && <td><button onClick={() => setEditing(r)}>Edit</button></td>}
              </tr>
            ))}
            {!q.isLoading && rows.length === 0 && (
              <tr><td colSpan={admin ? 9 : 8} className="muted">No rule matches that.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {paged.foot}

      {editing && <RuleSheet rule={editing} onClose={() => setEditing(null)} />}
      {adding && <NewRuleSheet rules={all} onClose={() => setAdding(false)} />}
      <p className="muted" style={{ marginTop: 16 }}>
        <Link to="/alarms">Back to alarms</Link>
      </p>
    </>
  );
}

// ------------------------------------------------------------ edit a rule

const num = (s: string): number | null => (s.trim() === '' ? null : Number(s));

/** The deadband, said the way the server says it. */
function bandProblem(op: string | null, thr: number | null, clr: number | null): string | null {
  if (thr == null || clr == null || Number.isNaN(thr) || Number.isNaN(clr)) return null;
  if ((op === '>' || op === '>=') && clr > thr) {
    return `Above the raise point: it would clear the moment it raised. Use ${thr} or lower.`;
  }
  if ((op === '<' || op === '<=') && clr < thr) {
    return `Below the raise point: it would clear the moment it raised. Use ${thr} or higher.`;
  }
  return null;
}

function RuleSheet({ rule, onClose }: { rule: AlarmRule; onClose: () => void }) {
  const qc = useQueryClient();
  const numeric = rule.metric_kind === 'numeric' && rule.operator != null;
  const [f, setF] = useState({
    threshold: rule.threshold == null ? '' : String(rule.threshold),
    clear_threshold: rule.clear_threshold == null ? '' : String(rule.clear_threshold),
    dwell_samples: String(rule.dwell_samples),
    dwell_seconds: rule.dwell_seconds == null ? '' : String(rule.dwell_seconds),
    clear_dwell_samples: String(rule.clear_dwell_samples),
    severity: rule.severity,
    message_tpl: rule.message_tpl,
  });
  const set = (k: keyof typeof f, v: string) => setF((x) => ({ ...x, [k]: v }));

  const patch = useMemo(() => {
    const p: AlarmRulePatch = {};
    if (numeric) {
      if (num(f.threshold) !== rule.threshold) p.threshold = num(f.threshold);
      if (num(f.clear_threshold) !== rule.clear_threshold) p.clear_threshold = num(f.clear_threshold);
    }
    if (Number(f.dwell_samples) !== rule.dwell_samples) p.dwell_samples = Number(f.dwell_samples);
    if (num(f.dwell_seconds) !== rule.dwell_seconds) p.dwell_seconds = num(f.dwell_seconds);
    if (Number(f.clear_dwell_samples) !== rule.clear_dwell_samples) {
      p.clear_dwell_samples = Number(f.clear_dwell_samples);
    }
    if (f.severity !== rule.severity) p.severity = f.severity;
    if (f.message_tpl.trim() !== rule.message_tpl) p.message_tpl = f.message_tpl.trim();
    return p;
  }, [f, rule, numeric]);
  const changed = Object.keys(patch).length;
  const band = numeric ? bandProblem(rule.operator, num(f.threshold), num(f.clear_threshold)) : null;
  const dwellBad = !(Number(f.dwell_samples) >= 1) || !(Number(f.clear_dwell_samples) >= 1);

  const save = useMutation({
    mutationFn: () => api.editAlarmRule(rule.id, patch),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['alarm-rules'] }); onClose(); },
  });
  const del = useMutation({
    mutationFn: () => api.deleteAlarmRule(rule.id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['alarm-rules'] }); onClose(); },
  });
  const u = unitOf(rule.metric_key);
  const err = save.error ?? del.error;

  return (
    <div className="sheet-scrim" role="dialog" aria-modal="true" aria-label={`Edit ${rule.name}`}
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="sheet narrow">
        <header className="sheet-head">
          <div>
            <h2>{rule.name}</h2>
            <p>
              {condition(rule)}. A change moves the alarm on every device this rule covers
              {rule.overrides ? `, except the ${rule.overrides} with their own override` : ''};
              workers pick it up on the next batch.
            </p>
          </div>
          <button className="close" onClick={onClose} aria-label="Close">✕</button>
        </header>
        <div className="sheet-body">
          {err && <div className="banner" role="alert">
            {err instanceof ApiError ? err.message : String((err as Error).message)}</div>}
          <div className="form-grid">
            {numeric ? (
              <>
                <label>
                  <span>Raises when {metricLabel(rule.metric_key ?? '')} {rule.operator}{u ? ` (${u})` : ''}</span>
                  <input className="mono" inputMode="decimal" value={f.threshold}
                         onChange={(e) => set('threshold', e.target.value)} />
                </label>
                <label>
                  <span>Clears at{u ? ` (${u})` : ''}</span>
                  <input className="mono" inputMode="decimal" value={f.clear_threshold}
                         onChange={(e) => set('clear_threshold', e.target.value)} />
                  <em className={band ? 'hint bad' : 'hint'}>
                    {band ?? 'The deadband: a reading resting on the limit must not flap. Leave empty for a default just inside the limit.'}
                  </em>
                </label>
              </>
            ) : (
              <p className="muted">This rule watches a state, so there is no number to move: only how
                long it must hold, how serious it is, and what it says.</p>
            )}
            <label>
              <span>Samples before it raises</span>
              <input className="mono" inputMode="numeric" value={f.dwell_samples}
                     onChange={(e) => set('dwell_samples', e.target.value)} />
              <em className="hint">Consecutive readings over the limit. Higher is quieter and slower.</em>
            </label>
            <label>
              <span>…and for at least (seconds)</span>
              <input className="mono" inputMode="numeric" value={f.dwell_seconds} placeholder="no minimum"
                     onChange={(e) => set('dwell_seconds', e.target.value)} />
            </label>
            <label>
              <span>Samples before it clears</span>
              <input className="mono" inputMode="numeric" value={f.clear_dwell_samples}
                     onChange={(e) => set('clear_dwell_samples', e.target.value)} />
            </label>
            <label>
              <span>Severity</span>
              <select value={f.severity} onChange={(e) => set('severity', e.target.value)}>
                {SEVERITIES.map((s) => <option key={s} value={s}>{s.toLowerCase()}</option>)}
              </select>
            </label>
            <label>
              <span>Message</span>
              <textarea rows={2} value={f.message_tpl} onChange={(e) => set('message_tpl', e.target.value)} />
              <em className="hint">May use {'{value}'}, {'{threshold}'}, {'{metric}'}, {'{alarm_type}'}.</em>
            </label>
          </div>
        </div>
        <div className="sheet-foot">
          {rule.custom && (
            <button className="danger" disabled={del.isPending}
                    onClick={() => { if (window.confirm(`Delete “${rule.name}”? Its open alarms stay until cleared.`)) del.mutate(); }}>
              Delete rule
            </button>
          )}
          <span className="muted">{changed ? `${changed} change${changed === 1 ? '' : 's'} to save` : 'Nothing changed'}</span>
          <span className="spacer" />
          <button onClick={onClose}>Cancel</button>
          <button className="primary" disabled={!changed || !!band || dwellBad || save.isPending}
                  onClick={() => save.mutate()}>
            {save.isPending ? 'Saving…' : 'Save changes'}
          </button>
        </div>
      </section>
    </div>
  );
}

// ------------------------------------------------------------- new rule

function NewRuleSheet({ rules, onClose }: { rules: AlarmRule[]; onClose: () => void }) {
  const qc = useQueryClient();
  // Every metric a numeric rule can watch, by its catalogue name.
  const metrics = useMemo(() => (Object.keys(METRICS) as MetricKey[])
    .filter((k) => METRICS[k].valueType === 'gauge')
    .sort((a, b) => METRICS[a].displayName.localeCompare(METRICS[b].displayName)), []);
  const types = useMemo(() => [...new Set(rules.flatMap((r) => r.device_types))].sort(), [rules]);
  const [f, setF] = useState({
    name: '', metric_key: 'cpu_utilization', operator: '>', threshold: '', clear_threshold: '',
    severity: 'WARNING', dwell_samples: '3', device_types: [] as string[],
  });
  const set = <K extends keyof typeof f>(k: K, v: (typeof f)[K]) => setF((x) => ({ ...x, [k]: v }));
  const m = METRICS[f.metric_key as MetricKey];
  const band = bandProblem(f.operator, num(f.threshold), num(f.clear_threshold));
  const ready = f.name.trim().length >= 3 && f.threshold.trim() !== '' && !band;

  const create = useMutation({
    mutationFn: () => {
      const body: AlarmRuleNew = {
        name: f.name.trim(), metric_key: f.metric_key, operator: f.operator,
        threshold: Number(f.threshold), clear_threshold: num(f.clear_threshold),
        severity: f.severity, device_types: f.device_types,
        dwell_samples: Number(f.dwell_samples) || 3,
      };
      return api.createAlarmRule(body);
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['alarm-rules'] }); onClose(); },
  });

  return (
    <div className="sheet-scrim" role="dialog" aria-modal="true" aria-label="New alarm rule"
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="sheet narrow">
        <header className="sheet-head">
          <div>
            <h2>New rule</h2>
            <p>A threshold on any reading the platform collects. It starts raising on the next batch.</p>
          </div>
          <button className="close" onClick={onClose} aria-label="Close">✕</button>
        </header>
        <div className="sheet-body">
          {create.error && <div className="banner" role="alert">{String((create.error as Error).message)}</div>}
          <div className="form-grid">
            <label><span>Name</span>
              <input value={f.name} placeholder="e.g. Hot GPU server CPU" onChange={(e) => set('name', e.target.value)} />
            </label>
            <label><span>Reading</span>
              <select value={f.metric_key} onChange={(e) => set('metric_key', e.target.value)}>
                {metrics.map((k) => <option key={k} value={k}>{METRICS[k].displayName} ({METRICS[k].unit})</option>)}
              </select>
            </label>
            <label><span>Raises when it is</span>
              <select value={f.operator} onChange={(e) => set('operator', e.target.value)}>
                <option value=">">above</option><option value=">=">at or above</option>
                <option value="<">below</option><option value="<=">at or below</option>
              </select>
            </label>
            <label><span>Threshold{m?.unit ? ` (${m.unit})` : ''}</span>
              <input className="mono" inputMode="decimal" value={f.threshold}
                     onChange={(e) => set('threshold', e.target.value)} />
            </label>
            <label><span>Clears at{m?.unit ? ` (${m.unit})` : ''}</span>
              <input className="mono" inputMode="decimal" value={f.clear_threshold} placeholder="just inside the limit"
                     onChange={(e) => set('clear_threshold', e.target.value)} />
              <em className={band ? 'hint bad' : 'hint'}>{band ?? 'The deadband that stops it flapping.'}</em>
            </label>
            <label><span>Severity</span>
              <select value={f.severity} onChange={(e) => set('severity', e.target.value)}>
                {SEVERITIES.map((s) => <option key={s} value={s}>{s.toLowerCase()}</option>)}
              </select>
            </label>
            <label><span>Samples before it raises</span>
              <input className="mono" inputMode="numeric" value={f.dwell_samples}
                     onChange={(e) => set('dwell_samples', e.target.value)} />
            </label>
            <fieldset style={{ border: 0, padding: 0 }}>
              <legend className="muted small">Applies to (none ticked: every device that reports it)</legend>
              <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginTop: 6 }}>
                {types.map((t) => (
                  <label key={t} className="switch" style={{ display: 'flex', gap: 6 }}>
                    <input type="checkbox" checked={f.device_types.includes(t)}
                           onChange={(e) => set('device_types', e.target.checked
                             ? [...f.device_types, t] : f.device_types.filter((x) => x !== t))} />
                    <span>{humanise(t)}</span>
                  </label>
                ))}
              </div>
            </fieldset>
          </div>
        </div>
        <div className="sheet-foot">
          <span className="spacer" />
          <button onClick={onClose}>Cancel</button>
          <button className="primary" disabled={!ready || create.isPending} onClick={() => create.mutate()}>
            {create.isPending ? 'Creating…' : 'Create rule'}
          </button>
        </div>
      </section>
    </div>
  );
}
