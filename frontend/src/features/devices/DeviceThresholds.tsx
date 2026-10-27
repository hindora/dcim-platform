import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ApiError, api, atLeast,
  type DeviceAlarmRule, type RuleOverrideBody,
} from '../../api/client';
import { usePaged } from '../../components/Pagination';
import { metricLabel } from '../../lib/format';
import { METRICS, type MetricKey } from '../../lib/metrics.gen';
import { Notice } from '../../components/Notice';

/** The alarm thresholds that apply to THIS device.
 *
 *  Estate policy lives on the rules page; a device that legitimately runs
 *  hot - a GPU node, a switch in a closed cabinet - gets its own value here
 *  rather than the whole estate's alarm being loosened for it. Each row shows
 *  the estate's value and, when the device has an exception, the device's.
 *  Muting a rule on a device is the strongest exception, so it asks why: a
 *  muted alarm is one nobody will see, and the reason is what the next person
 *  needs to know whether to restore it.
 *
 *  An operator may set one: it is scoped to one device, reversible, and
 *  audited with its before and after. */

const SEVERITIES = ['INFO', 'WARNING', 'MINOR', 'MAJOR', 'CRITICAL'];
const unit = (k: string) => METRICS[k as MetricKey]?.unit ?? '';
const val = (v: number | null, k: string) => (v == null ? '–' : `${v}${unit(k) ? ` ${unit(k)}` : ''}`);

function effective(r: DeviceAlarmRule) {
  return {
    threshold: r.o_threshold ?? r.threshold,
    severity: r.o_severity ?? r.severity,
    muted: r.o_enabled === false,
  };
}

export function DeviceThresholds({ deviceId }: { deviceId: string }) {
  const canEdit = atLeast('operator');
  const q = useQuery({
    queryKey: ['device-alarm-rules', deviceId],
    queryFn: () => api.deviceAlarmRules(deviceId),
    staleTime: 30_000,
  });
  const [editing, setEditing] = useState<DeviceAlarmRule | null>(null);
  const rules = useMemo(() => q.data?.items ?? [], [q.data]);
  // Rules this device has an exception to come first: they are the ones that
  // differ from what the estate page says.
  const sorted = useMemo(() => [...rules].sort((a, b) =>
    Number(b.overridden) - Number(a.overridden)), [rules]);
  const paged = usePaged(sorted, { noun: 'rules' });
  const overridden = rules.filter((r) => r.overridden).length;

  if (q.isLoading || rules.length === 0) return null;
  return (
    <section>
      <h3>Alarm thresholds<span className="count"> {rules.length}</span></h3>
      <p className="muted small">
        {overridden
          ? `${overridden} rule${overridden === 1 ? '' : 's'} on this device differ${overridden === 1 ? 's' : ''} from the estate's.`
          : 'This device follows the estate’s thresholds.'}
      </p>
      <div className="asset-scroll">
        <table>
          <thead>
            <tr><th>Rule</th><th>Watches</th><th>Estate</th><th>This device</th><th>Severity</th>{canEdit && <th />}</tr>
          </thead>
          <tbody>
            {paged.rows.map((r) => {
              const e = effective(r);
              return (
                <tr key={r.id} className={r.enabled ? undefined : 'muted'}>
                  <td>{r.name}{!r.enabled && <small className="muted"> · rule is off</small>}</td>
                  <td className="muted">{metricLabel(r.metric_key)} {r.operator}</td>
                  <td className="muted">{val(r.threshold, r.metric_key)}</td>
                  <td>
                    {e.muted
                      ? <span className="vw-warn" title={r.o_reason ?? ''}>muted · {r.o_reason}</span>
                      : r.overridden && r.o_threshold != null
                        ? <b>{val(e.threshold, r.metric_key)}</b>
                        : <span className="muted">follows the rule</span>}
                  </td>
                  <td>{e.severity.toLowerCase()}{r.o_severity && r.o_severity !== r.severity ? ' *' : ''}</td>
                  {canEdit && <td><button onClick={() => setEditing(r)}>{r.overridden ? 'Change' : 'Override'}</button></td>}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {paged.foot}
      {editing && <OverrideSheet deviceId={deviceId} rule={editing} onClose={() => setEditing(null)} />}
    </section>
  );
}

function OverrideSheet({ deviceId, rule, onClose }: {
  deviceId: string; rule: DeviceAlarmRule; onClose: () => void;
}) {
  const qc = useQueryClient();
  const s = (v: number | null) => (v == null ? '' : String(v));
  const [f, setF] = useState({
    threshold: s(rule.o_threshold), clear_threshold: s(rule.o_clear_threshold),
    severity: rule.o_severity ?? '', dwell_samples: s(rule.o_dwell_samples),
    mute: rule.o_enabled === false, reason: rule.o_reason ?? '',
  });
  const set = <K extends keyof typeof f>(k: K, v: (typeof f)[K]) => setF((x) => ({ ...x, [k]: v }));
  const n = (t: string): number | null => (t.trim() === '' ? null : Number(t));

  const thr = n(f.threshold) ?? rule.threshold;
  const clr = n(f.clear_threshold) ?? (f.threshold.trim() === '' ? rule.clear_threshold : null);
  const band =
    thr != null && clr != null && ((['>', '>='].includes(rule.operator) && clr > thr)
      || (['<', '<='].includes(rule.operator) && clr < thr))
      ? `The clear point must be on the safe side of ${thr}: it would clear the moment it raised.` : null;
  const empty = f.threshold.trim() === '' && f.clear_threshold.trim() === '' && f.severity === ''
    && f.dwell_samples.trim() === '' && !f.mute;
  const needReason = f.mute && f.reason.trim() === '';

  const body: RuleOverrideBody = {
    threshold: n(f.threshold), clear_threshold: n(f.clear_threshold),
    severity: f.severity || null, dwell_samples: n(f.dwell_samples),
    enabled: f.mute ? false : null, reason: f.mute ? f.reason.trim() : null,
  };
  const done = () => {
    qc.invalidateQueries({ queryKey: ['device-alarm-rules', deviceId] });
    qc.invalidateQueries({ queryKey: ['alarm-rules'] });
    onClose();
  };
  const save = useMutation({ mutationFn: () => api.putRuleOverride(deviceId, rule.id, body), onSuccess: done });
  const remove = useMutation({ mutationFn: () => api.removeRuleOverride(deviceId, rule.id), onSuccess: done });
  const err = save.error ?? remove.error;
  const u = unit(rule.metric_key);

  return (
    <div className="sheet-scrim" role="dialog" aria-modal="true" aria-label={`Override ${rule.name}`}
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="sheet narrow">
        <header className="sheet-head">
          <div>
            <h2>{rule.name}</h2>
            <p>
              The estate raises this when {metricLabel(rule.metric_key)} {rule.operator} {val(rule.threshold, rule.metric_key)}.
              Anything you leave empty follows the estate. This applies to this device only.
            </p>
          </div>
          <button className="close" onClick={onClose} aria-label="Close">✕</button>
        </header>
        <div className="sheet-body">
          {err && <Notice tone="error">
            {err instanceof ApiError ? err.message : String((err as Error).message)}</Notice>}
          <div className="form-grid">
            <label>
              <span>Raises when {rule.operator}{u ? ` (${u})` : ''}</span>
              <input className="mono" inputMode="decimal" value={f.threshold}
                     placeholder={`${rule.threshold ?? ''} (estate)`} disabled={f.mute}
                     onChange={(e) => set('threshold', e.target.value)} />
            </label>
            <label>
              <span>Clears at{u ? ` (${u})` : ''}</span>
              <input className="mono" inputMode="decimal" value={f.clear_threshold}
                     placeholder={f.threshold.trim() ? 'just inside the new limit' : `${rule.clear_threshold ?? ''} (estate)`}
                     disabled={f.mute} onChange={(e) => set('clear_threshold', e.target.value)} />
              <em className={band ? 'hint bad' : 'hint'}>{band ?? 'Moves with the raise point.'}</em>
            </label>
            <label>
              <span>Severity</span>
              <select value={f.severity} disabled={f.mute} onChange={(e) => set('severity', e.target.value)}>
                <option value="">{rule.severity.toLowerCase()} (estate)</option>
                {SEVERITIES.map((v) => <option key={v} value={v}>{v.toLowerCase()}</option>)}
              </select>
            </label>
            <label>
              <span>Samples before it raises</span>
              <input className="mono" inputMode="numeric" value={f.dwell_samples} disabled={f.mute}
                     placeholder={`${rule.dwell_samples} (estate)`}
                     onChange={(e) => set('dwell_samples', e.target.value)} />
            </label>
            <label className="switch">
              <span>Mute this rule on this device</span>
              <input type="checkbox" checked={f.mute} onChange={(e) => set('mute', e.target.checked)} />
              <em className="hint">A muted alarm is one nobody will see. Say why.</em>
            </label>
            {f.mute && (
              <label>
                <span>Reason</span>
                <input value={f.reason} placeholder="e.g. decommissioning on the 28th, ticket CHG-1042"
                       onChange={(e) => set('reason', e.target.value)} />
              </label>
            )}
          </div>
        </div>
        <div className="sheet-foot">
          {rule.overridden && (
            <button disabled={remove.isPending} onClick={() => remove.mutate()}>Follow the estate again</button>
          )}
          <span className="spacer" />
          <button onClick={onClose}>Cancel</button>
          <button className="primary" disabled={empty || !!band || needReason || save.isPending}
                  onClick={() => save.mutate()}>
            {save.isPending ? 'Saving…' : 'Save override'}
          </button>
        </div>
      </section>
    </div>
  );
}
