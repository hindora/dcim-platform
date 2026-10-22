import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  ApiError, api, atLeast,
  type AnomalyDetector, type AnomalyOverride, type AnomalyParamValue,
} from '../../../api/client';
import { usePaged } from '../../../components/Pagination';
import { humanise, relativeTime } from '../../../lib/format';
import { Dialog, DialogActions } from '../../assets/components/Dialog';
import { fromLocalInput, paramText, stamp } from '../../analytics/anomaly/format';
import { DeviceSearch, type PickedDevice } from './DeviceSearch';
import { ParamFields, anyInvalid, effectiveValues, fieldError } from './ParamForm';

/** Per-device sensitivity or mute (doc 31 section 9.5). An operator's
 *  action, like an alarm-rule override: one device that needs its own value
 *  gets it here, the estate keeps the detector's. */

export function useAnomalyOverrides() {
  return useQuery({
    queryKey: ['anomaly-overrides'],
    queryFn: () => api.anomalyOverrides({ limit: '500', with_total: 'true' }),
    staleTime: 30_000,
  });
}

function isExpired(o: AnomalyOverride): boolean {
  return Boolean(o.expires_at) && new Date(o.expires_at as string).getTime() < Date.now();
}

function effect(o: AnomalyOverride, d: AnomalyDetector | undefined): string {
  if (o.enabled === false) return 'Muted';
  const parts = Object.entries(o.params ?? {}).map(([k, v]) => {
    const spec = d?.param_schema.find((s) => s.key === k);
    const base = d ? (d.params[k] ?? spec?.default) : undefined;
    return `${spec?.label ?? k} ${paramText(spec, base)} → ${paramText(spec, v)}`;
  });
  if (parts.length) return parts.join(' · ');
  return o.enabled === true ? 'Enabled on this device' : '—';
}

export function OverridesTab({ detectors }: { detectors: AnomalyDetector[] }) {
  const canEdit = atLeast('operator');
  const q = useAnomalyOverrides();
  const byId = useMemo(() => new Map(detectors.map((d) => [d.id, d])), [detectors]);
  const rows = q.data?.items ?? [];
  const paged = usePaged(rows, { noun: 'overrides', always: true });
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<AnomalyOverride | null>(null);

  return (
    <>
      <div className="anom-filters">
        <span className="muted small">
          A device that needs its own sensitivity, or a mute with a reason. Prefer an
          exclusion window for planned work.
        </span>
        <span className="spacer" />
        {canEdit
          ? <button type="button" className="primary" onClick={() => setAdding(true)}>Add override</button>
          : <span className="muted small">Adding overrides needs an operator.</span>}
      </div>

      {q.isLoading && <p className="muted">Loading…</p>}
      {q.isError && <div className="banner" role="alert">Could not load overrides: {(q.error as Error).message}</div>}

      {q.data && (
        <div className="asset-scroll">
          <table className="anom-table">
            <thead>
              <tr>
                <th>Device</th><th>Detector</th><th>Effect</th><th>Reason</th>
                <th>Expires</th><th>Set by</th>{canEdit && <th />}
              </tr>
            </thead>
            <tbody>
              {paged.rows.map((o) => {
                const d = byId.get(o.detector_id);
                const expired = isExpired(o);
                return (
                  <tr key={`${o.detector_id}/${o.device_id}`} className={expired ? 'is-expired' : undefined}>
                    <td><Link to={`/devices/${o.device_id}`}>{o.device_name}</Link></td>
                    <td><span className="anom-id">{o.detector_id}</span> <span className="muted">{d?.name ?? ''}</span></td>
                    <td>{effect(o, d)}</td>
                    <td className="muted">{o.reason || '—'}</td>
                    <td className="muted">
                      {o.expires_at ? stamp(o.expires_at) : 'never'}
                      {expired && <div className="small">expired — ignored</div>}
                    </td>
                    <td className="muted">{o.updated_by} · {relativeTime(o.updated_at)}</td>
                    {canEdit && (
                      <td><button type="button" onClick={() => setRemoving(o)}>Remove override</button></td>
                    )}
                  </tr>
                );
              })}
              {rows.length === 0 && (
                <tr><td colSpan={canEdit ? 7 : 6} className="muted">
                  No overrides. Every device runs each detector at the estate’s settings.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
      {paged.foot}

      {adding && <AddOverride detectors={detectors} existing={rows} onClose={() => setAdding(false)} />}
      {removing && <RemoveOverride o={removing} detector={byId.get(removing.detector_id)}
                                   onClose={() => setRemoving(null)} />}
    </>
  );
}

function RemoveOverride({ o, detector, onClose }: {
  o: AnomalyOverride; detector?: AnomalyDetector; onClose: () => void;
}) {
  const qc = useQueryClient();
  const del = useMutation({
    mutationFn: () => api.removeAnomalyOverride(o.detector_id, o.device_id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['anomaly-overrides'] }); onClose(); },
  });
  return (
    <Dialog title="Remove override?" onClose={onClose}>
      <p className="anom-dialog-copy">
        {o.detector_id} {detector ? `(${detector.name}) ` : ''}on {o.device_name} goes back to
        the detector’s own settings at the next sweep.
      </p>
      {del.error && <p className="anom-err" role="alert">{(del.error as Error).message}</p>}
      <DialogActions>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" className="danger" disabled={del.isPending} onClick={() => del.mutate()}>
          {del.isPending ? 'Removing…' : 'Remove override'}
        </button>
      </DialogActions>
    </Dialog>
  );
}

function AddOverride({ detectors, existing, onClose }: {
  detectors: AnomalyDetector[]; existing: AnomalyOverride[]; onClose: () => void;
}) {
  const qc = useQueryClient();
  const perDevice = useMemo(() => detectors.filter((d) => d.id !== 'L5' && d.device_types.length),
                            [detectors]);
  const coveredTypes = useMemo(() => [...new Set(perDevice.flatMap((d) => d.device_types))].sort(),
                               [perDevice]);
  const [device, setDevice] = useState<PickedDevice | null>(null);
  const [detectorId, setDetectorId] = useState('');
  const [mode, setMode] = useState<'mute' | 'params'>('params');
  const [values, setValues] = useState<Record<string, AnomalyParamValue>>({});
  const [reason, setReason] = useState('');
  const [expires, setExpires] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});

  const options = device ? perDevice.filter((d) => d.device_types.includes(device.device_type)) : [];
  const det = options.find((d) => d.id === detectorId);
  const prior = det && device
    ? existing.find((o) => o.detector_id === det.id && o.device_id === device.id) : undefined;
  // What the device runs today: the detector's values, then its override.
  const effective = useMemo(
    () => (det ? effectiveValues(det.param_schema, det.params, prior?.params) : {}),
    [det, prior]);

  const pickDetector = (id: string) => {
    setDetectorId(id);
    const d = options.find((x) => x.id === id);
    const p = d && device ? existing.find((o) => o.detector_id === d.id && o.device_id === device.id) : undefined;
    setValues(d ? effectiveValues(d.param_schema, d.params, p?.params) : {});
    setErrors({});
  };

  const specs = det?.param_schema ?? [];
  const base = det ? effectiveValues(specs, det.params) : {};
  // PUT replaces the override's params, so send every value that differs
  // from the DETECTOR's (the prior override's included), not only the edits.
  const sendParams = Object.fromEntries(specs.filter((s) => values[s.key] !== base[s.key])
    .map((s) => [s.key, values[s.key]]));
  const differs = specs.some((s) => values[s.key] !== effective[s.key]);
  const invalid = anyInvalid(specs, values);
  const expiresIso = fromLocalInput(expires);
  const expiresBad = Boolean(expires) && (!expiresIso || new Date(expiresIso).getTime() <= Date.now());

  let blocked: string | null = null;
  if (!device) blocked = 'Choose a device.';
  else if (!det) blocked = 'Choose a detector.';
  else if (mode === 'mute' && !reason.trim()) blocked = 'A mute must say why.';
  else if (mode === 'mute' && prior?.enabled === false && (prior.reason ?? '') === reason.trim()
           && (prior.expires_at ?? null) === expiresIso) blocked = 'It is already muted like this.';
  else if (mode === 'params' && !differs) blocked = 'Nothing differs from what this device runs now.';
  else if (mode === 'params' && !Object.keys(sendParams).length) {
    blocked = 'Every value matches the detector’s own. Remove the override instead.';
  } else if (mode === 'params' && invalid) blocked = 'A value is out of range.';
  else if (expiresBad) blocked = 'Expiry must be in the future.';

  const save = useMutation({
    mutationFn: () => api.putAnomalyOverride(det!.id, device!.id, mode === 'mute'
      ? { enabled: false, reason: reason.trim(), expires_at: expiresIso }
      : { params: sendParams, reason: reason.trim() || undefined, expires_at: expiresIso }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['anomaly-overrides'] });
      qc.invalidateQueries({ queryKey: ['anomaly-detectors'] });
      onClose();
    },
    onError: (e) => {
      const fe = fieldError(e);
      if (e instanceof ApiError && e.detail?.error === 'mute_needs_reason') fe.reason = e.message;
      setErrors(fe);
    },
  });
  const general = save.error && !Object.keys(errors).length ? (save.error as Error).message : null;

  return (
    <Dialog title="Add override" onClose={onClose} wide>
      <div className="anom-form">
        <div>
          <span className="lbl">Device</span>
          <DeviceSearch id="ovr-device" value={device} types={coveredTypes}
                        onChange={(d) => { setDevice(d); setDetectorId(''); setValues({}); }} />
        </div>
        <label>
          <span>Detector</span>
          <select value={detectorId} disabled={!device} onChange={(e) => pickDetector(e.target.value)}>
            <option value="">{device ? 'Choose a detector' : 'Choose a device first'}</option>
            {options.map((d) => (
              <option key={d.id} value={d.id}>{d.id} — {d.name}</option>
            ))}
          </select>
          {device && !options.length && (
            <em className="anom-note">No detector covers a {humanise(device.device_type)}.</em>
          )}
        </label>
        <div className="radios" role="radiogroup" aria-label="Override kind">
          <label>
            <input type="radio" name="ovr-mode" checked={mode === 'params'} onChange={() => setMode('params')} />
            <span>Change parameters</span>
          </label>
          <label>
            <input type="radio" name="ovr-mode" checked={mode === 'mute'} onChange={() => setMode('mute')} />
            <span>Mute on this device</span>
          </label>
        </div>
        {mode === 'params' && det && (
          <ParamFields specs={specs} values={values} base={effective} idPrefix={`ovr-${det.id}`}
                       errors={errors}
                       onChange={(k, v) => {
                         setValues((x) => ({ ...x, [k]: v }));
                         setErrors((er) => { const n = { ...er }; delete n[k]; return n; });
                       }} />
        )}
        <label>
          <span>Reason{mode === 'mute' ? '' : ' (recommended)'}</span>
          <textarea className="anom-textarea" maxLength={1000} value={reason}
                    aria-invalid={Boolean(errors.reason) || undefined}
                    onChange={(e) => setReason(e.target.value)} />
          {errors.reason && <span className="anom-err" role="alert">{errors.reason}</span>}
        </label>
        <label>
          <span>Expires (optional)</span>
          <input type="datetime-local" value={expires} onChange={(e) => setExpires(e.target.value)} />
        </label>
        <p className="anom-note">
          Muted detectors raise nothing on this device, and its open {det?.id ?? 'detector'} findings
          are resolved as muted. Prefer an exclusion window for planned work.
        </p>
      </div>
      {general && <p className="anom-err" role="alert">{general}</p>}
      <DialogActions>
        <span className="muted small" aria-live="polite">{blocked ?? ''}</span>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" className="primary" disabled={Boolean(blocked) || save.isPending}
                onClick={() => save.mutate()}>
          {save.isPending ? 'Saving…' : mode === 'mute' ? 'Mute on this device' : 'Save override'}
        </button>
      </DialogActions>
    </Dialog>
  );
}
