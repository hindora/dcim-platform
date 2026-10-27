import { useMemo, useState } from 'react';
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import {
  ApiError, api, atLeast,
  type AnomalyDetector, type AnomalyExclusion, type ExclusionSource,
} from '../../../api/client';
import { usePaged } from '../../../components/Pagination';
import { relativeTime } from '../../../lib/format';
import { METRICS, type MetricKey } from '../../../lib/metrics.gen';
import { Dialog, DialogActions } from '../../assets/components/Dialog';
import {
  EXCLUSION_SOURCE_WORD, duration, fromLocalInput, stamp, toLocalInput,
} from '../../analytics/anomaly/format';
import { DeviceSearch, type PickedDevice } from './DeviceSearch';
import { Notice } from '../../../components/Notice';

/** Spans the nightly baseline fit must not learn as normal (doc 31 section
 *  9.6). An exclusion does NOT silence anything: it keeps a maintenance
 *  window or a confirmed fault out of the next fit, so the refit does not
 *  learn the fault as normal. Maintenance and confirmed-fault rows follow
 *  their source and cannot be deleted here. */

const SOURCES: ExclusionSource[] = ['manual', 'maintenance', 'confirmed_finding', 'injected_fault'];
const SYSTEM_OWNED: ExclusionSource[] = ['maintenance', 'confirmed_finding'];

export function useAnomalyExclusions() {
  return useQuery({
    queryKey: ['anomaly-exclusions'],
    queryFn: () => api.anomalyExclusions({ limit: '500', with_total: 'true' }),
    staleTime: 30_000,
  });
}

function isActive(x: AnomalyExclusion, now: number): boolean {
  return new Date(x.starts_at).getTime() <= now
    && (!x.ends_at || new Date(x.ends_at).getTime() > now);
}

export function ExclusionsTab({ detectors }: { detectors: AnomalyDetector[] }) {
  const canEdit = atLeast('operator');
  const [params, setParams] = useSearchParams();
  const q = useAnomalyExclusions();
  const opts = useQuery({ queryKey: ['asset-filter-options'], queryFn: api.assetFilterOptions,
                          staleTime: 5 * 60_000 });
  const [adding, setAdding] = useState(false);
  const [deleting, setDeleting] = useState<AnomalyExclusion | null>(null);

  const on = (params.get('source') ?? '').split(',').filter((s): s is ExclusionSource =>
    (SOURCES as string[]).includes(s));
  const activeOnly = params.get('active') === 'true';
  const setFilter = (next: ExclusionSource[], active: boolean) => {
    const p = new URLSearchParams(params);
    if (next.length && next.length < SOURCES.length) p.set('source', next.join(','));
    else p.delete('source');
    if (active) p.set('active', 'true'); else p.delete('active');
    setParams(p, { replace: true });
  };

  const all = q.data?.items ?? [];
  const now = Date.now();
  const scoped = activeOnly ? all.filter((x) => isActive(x, now)) : all;
  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const x of scoped) c[x.source] = (c[x.source] ?? 0) + 1;
    return c;
  }, [scoped]);
  const rows = on.length ? scoped.filter((x) => on.includes(x.source)) : scoped;
  const paged = usePaged(rows, { noun: 'exclusions', always: true });

  // Names for the devices on this page only: the list carries ids.
  const deviceIds = [...new Set(paged.rows.map((x) => x.device_id).filter(Boolean) as string[])];
  const names = useQueries({
    queries: deviceIds.map((id) => ({
      queryKey: ['device', id], queryFn: () => api.device(id), staleTime: 5 * 60_000,
    })),
  });
  const deviceName = new Map(deviceIds.map((id, i) => [id, names[i]?.data?.name]));
  const roomName = new Map((opts.data?.rooms ?? []).map((r) => [r.id, `${r.name} (${r.dc})`]));
  const siteName = new Map((opts.data?.sites ?? []).map((s) => [s.id, s.code]));

  const scope = (x: AnomalyExclusion) => {
    const head = x.device_id
      ? <Link to={`/devices/${x.device_id}`}>{deviceName.get(x.device_id) ?? 'Device'}</Link>
      : x.room_id ? <span>Room {roomName.get(x.room_id) ?? x.room_id.slice(0, 8)}</span>
        : x.datacenter_id ? <span>Site {siteName.get(x.datacenter_id) ?? x.datacenter_id.slice(0, 8)}</span>
          : <span className="muted">—</span>;
    return (
      <>
        {head}
        {x.detector_id && <> · <span className="anom-id">{x.detector_id}</span></>}
        {x.metric_key && <> · <span className="mono small">{x.metric_key}</span></>}
      </>
    );
  };

  const sourceRef = (x: AnomalyExclusion) => {
    if (!x.source_ref) return null;
    if (x.source === 'maintenance') {
      return <Link to={`/assets/maintenance/${x.source_ref}`}>window</Link>;
    }
    return <span className="mono small">{x.source_ref}</span>;
  };

  return (
    <>
      <div className="anom-filters">
        <div className="seg facet-seg" role="group" aria-label="Source filter">
          <button type="button" className={on.length === 0 ? 'active' : ''}
                  aria-pressed={on.length === 0} onClick={() => setFilter([], activeOnly)}>
            All
          </button>
          {SOURCES.map((s) => {
            const pressed = on.includes(s);
            return (
              <button key={s} type="button" className={pressed ? 'active' : ''} aria-pressed={pressed}
                      onClick={() => setFilter(pressed ? on.filter((x) => x !== s) : [...on, s], activeOnly)}>
                {s === 'injected_fault' ? 'Injected' : EXCLUSION_SOURCE_WORD[s].replace(' window', '')}
                <b>{counts[s] ?? 0}</b>
              </button>
            );
          })}
        </div>
        <label className="muted small" style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
          <input type="checkbox" checked={activeOnly} style={{ width: 14, margin: 0 }}
                 onChange={(e) => setFilter(on, e.target.checked)} />
          Active now
        </label>
        <span className="spacer" />
        {canEdit
          ? <button type="button" className="primary" onClick={() => setAdding(true)}>Add exclusion</button>
          : <span className="muted small">Adding exclusions needs an operator.</span>}
      </div>

      {q.isLoading && <p className="muted">Loading…</p>}
      {q.isError && <Notice tone="error">Could not load exclusions: {(q.error as Error).message}</Notice>}

      {q.data && (
        <div className="asset-scroll">
          <table className="anom-table">
            <thead>
              <tr>
                <th>Scope</th><th>Window</th><th>Source</th><th>Reason</th><th>Created by</th>
                {canEdit && <th />}
              </tr>
            </thead>
            <tbody>
              {paged.rows.map((x) => {
                const ms = (x.ends_at ? new Date(x.ends_at).getTime() : now) - new Date(x.starts_at).getTime();
                const owned = SYSTEM_OWNED.includes(x.source);
                return (
                  <tr key={x.id}>
                    <td>{scope(x)}</td>
                    <td>
                      <span className="anom-num">{stamp(x.starts_at)} – {x.ends_at ? stamp(x.ends_at) : ''}</span>
                      {!x.ends_at && <span className="muted"> open — still excluding</span>}
                      <div className="muted small">{duration(ms / 1000)}{isActive(x, now) ? ' · active now' : ''}</div>
                    </td>
                    <td>
                      {EXCLUSION_SOURCE_WORD[x.source] ?? x.source}
                      {x.source_ref && <div className="small">{sourceRef(x)}</div>}
                    </td>
                    <td className="muted">{x.reason}</td>
                    <td className="muted">
                      {x.created_by || 'system'}
                      {x.created_at && <div className="small">{relativeTime(x.created_at)}</div>}
                    </td>
                    {canEdit && (
                      <td>
                        {owned
                          ? <span className="muted small" title="It follows its maintenance window or finding">Managed by its source</span>
                          : <button type="button" onClick={() => setDeleting(x)}>Delete</button>}
                      </td>
                    )}
                  </tr>
                );
              })}
              {rows.length === 0 && (
                <tr><td colSpan={canEdit ? 6 : 5} className="muted">
                  {all.length
                    ? 'No exclusion matches these filters.'
                    : 'No exclusions. Every window counts toward the next baseline fit.'}
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
      {paged.foot}

      {adding && <AddExclusion detectors={detectors} onClose={() => setAdding(false)} />}
      {deleting && <DeleteExclusion x={deleting} onClose={() => setDeleting(null)} />}
    </>
  );
}

function DeleteExclusion({ x, onClose }: { x: AnomalyExclusion; onClose: () => void }) {
  const qc = useQueryClient();
  const del = useMutation({
    mutationFn: () => api.deleteAnomalyExclusion(x.id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['anomaly-exclusions'] }); onClose(); },
  });
  const owned = del.error instanceof ApiError && del.error.status === 409;
  return (
    <Dialog title="Delete exclusion?" onClose={onClose}>
      <p className="anom-dialog-copy">
        The window {stamp(x.starts_at)} – {x.ends_at ? stamp(x.ends_at) : 'open'} counts toward
        the next nightly fit again. Baselines already fitted do not change.
      </p>
      {del.error && (
        <Notice tone="error">
          {owned
            ? `It is managed by its source: ${(del.error as Error).message}. Change the maintenance window or the finding instead.`
            : (del.error as Error).message}
        </Notice>
      )}
      <DialogActions>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" className="danger" disabled={del.isPending || owned} onClick={() => del.mutate()}>
          {del.isPending ? 'Deleting…' : 'Delete'}
        </button>
      </DialogActions>
    </Dialog>
  );
}

type ScopeKind = 'device' | 'room' | 'site';

function AddExclusion({ detectors, onClose }: { detectors: AnomalyDetector[]; onClose: () => void }) {
  const qc = useQueryClient();
  const opts = useQuery({ queryKey: ['asset-filter-options'], queryFn: api.assetFilterOptions,
                          staleTime: 5 * 60_000 });
  const [kind, setKind] = useState<ScopeKind>('device');
  const [device, setDevice] = useState<PickedDevice | null>(null);
  const [room, setRoom] = useState('');
  const [site, setSite] = useState('');
  const [detectorId, setDetectorId] = useState('');
  const [metric, setMetric] = useState('');
  const [starts, setStarts] = useState(() => toLocalInput(new Date().toISOString()));
  const [ends, setEnds] = useState('');
  const [reason, setReason] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});

  const startIso = fromLocalInput(starts);
  const endIso = fromLocalInput(ends);
  const datesBad = Boolean(startIso && endIso) && new Date(endIso as string) <= new Date(startIso as string);
  const scopeId = kind === 'device' ? device?.id : kind === 'room' ? room : site;
  const metricKeys = useMemo(() => (Object.keys(METRICS) as MetricKey[]).sort(), []);
  const metricBad = Boolean(metric) && !(metric in METRICS);

  let blocked: string | null = null;
  if (!scopeId) blocked = `Choose a ${kind}.`;
  else if (!startIso) blocked = 'Choose when it starts.';
  else if (datesBad) blocked = 'End must be after start';
  else if (metricBad) blocked = 'That metric is not in the registry.';
  else if (!reason.trim()) blocked = 'Say why this window is excluded.';

  const save = useMutation({
    mutationFn: () => api.createAnomalyExclusion({
      device_id: kind === 'device' ? device?.id : undefined,
      room_id: kind === 'room' ? room : undefined,
      datacenter_id: kind === 'site' ? site : undefined,
      detector_id: detectorId || undefined,
      metric_key: metric || undefined,
      starts_at: startIso as string,
      ends_at: endIso,
      reason: reason.trim(),
    }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['anomaly-exclusions'] }); onClose(); },
    onError: (e) => {
      const code = e instanceof ApiError ? e.detail?.error : undefined;
      if (code === 'exclusion_scope') setErrors({ scope: (e as Error).message });
      else if (code === 'exclusion_dates_invalid') setErrors({ ends: 'End must be after start' });
      else if (code === 'unknown_metric') setErrors({ metric: (e as Error).message });
      else setErrors({});
    },
  });
  const general = save.error && !Object.keys(errors).length ? (save.error as Error).message : null;

  return (
    <Dialog title="Add exclusion" onClose={onClose} wide>
      <div className="anom-form">
        <div>
          <span className="lbl" id="exc-scope-lbl">Scope — exactly one</span>
          <div className="radios" role="radiogroup" aria-labelledby="exc-scope-lbl">
            {(['device', 'room', 'site'] as ScopeKind[]).map((k) => (
              <label key={k}>
                <input type="radio" name="exc-kind" checked={kind === k} onChange={() => setKind(k)} />
                <span>{k === 'device' ? 'A device' : k === 'room' ? 'A room' : 'A site'}</span>
              </label>
            ))}
          </div>
        </div>
        {kind === 'device' && <DeviceSearch id="exc-device" value={device} onChange={setDevice} />}
        {kind === 'room' && (
          <label>
            <span>Room</span>
            <select value={room} onChange={(e) => setRoom(e.target.value)}>
              <option value="">Choose a room</option>
              {(opts.data?.rooms ?? []).map((r) => (
                <option key={r.id} value={r.id}>{r.dc} · {r.name}</option>
              ))}
            </select>
          </label>
        )}
        {kind === 'site' && (
          <label>
            <span>Site</span>
            <select value={site} onChange={(e) => setSite(e.target.value)}>
              <option value="">Choose a site</option>
              {(opts.data?.sites ?? []).map((s) => <option key={s.id} value={s.id}>{s.code}</option>)}
            </select>
          </label>
        )}
        {errors.scope && <p className="anom-err" role="alert">{errors.scope}</p>}

        <div className="row2">
          <label>
            <span>Only this detector (optional)</span>
            <select value={detectorId} onChange={(e) => setDetectorId(e.target.value)}>
              <option value="">Every detector</option>
              {detectors.filter((d) => d.id !== 'L5').map((d) => (
                <option key={d.id} value={d.id}>{d.id} — {d.name}</option>
              ))}
            </select>
          </label>
          <label>
            <span>Only this metric (optional)</span>
            <input list="exc-metrics" value={metric} placeholder="e.g. filter_diff_pressure"
                   aria-invalid={metricBad || Boolean(errors.metric) || undefined}
                   onChange={(e) => setMetric(e.target.value.trim())} />
            <datalist id="exc-metrics">
              {metricKeys.map((k) => <option key={k} value={k}>{METRICS[k].displayName}</option>)}
            </datalist>
            {(errors.metric || metricBad) && (
              <span className="anom-err" role="alert">{errors.metric ?? 'Not a registry metric key.'}</span>
            )}
          </label>
        </div>

        <div className="row2">
          <label>
            <span>Starts</span>
            <input type="datetime-local" value={starts} onChange={(e) => setStarts(e.target.value)} />
          </label>
          <label>
            <span>Ends</span>
            <input type="datetime-local" value={ends} aria-invalid={datesBad || undefined}
                   aria-describedby="exc-ends-hint" onChange={(e) => setEnds(e.target.value)} />
            <span id="exc-ends-hint" className={datesBad || errors.ends ? 'anom-err' : 'anom-note'}
                  role={datesBad || errors.ends ? 'alert' : undefined}>
              {datesBad || errors.ends ? 'End must be after start'
                : 'leave empty to keep excluding until you close it'}
            </span>
          </label>
        </div>

        <label>
          <span>Reason</span>
          <textarea className="anom-textarea" maxLength={1000} value={reason}
                    onChange={(e) => setReason(e.target.value)} />
        </label>
        <p className="anom-note">
          Excluding a window removes it from the next nightly fit at 03:00 UTC. It does not hide
          findings and does not change baselines already fitted.
        </p>
      </div>
      {general && <Notice tone="error">{general}</Notice>}
      <DialogActions>
        <span className="muted small" aria-live="polite">{blocked ?? ''}</span>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" className="primary" disabled={Boolean(blocked) || save.isPending}
                onClick={() => save.mutate()}>
          {save.isPending ? 'Adding…' : 'Add exclusion'}
        </button>
      </DialogActions>
    </Dialog>
  );
}
