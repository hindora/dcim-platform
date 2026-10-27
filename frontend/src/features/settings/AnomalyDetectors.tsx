import { Fragment, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import {
  api, atLeast, type AnomalyDetector, type AnomalyHealth, type DetectorLayer,
} from '../../api/client';
import { usePaged } from '../../components/Pagination';
import { StatusChip } from '../../components/StatusChip';
import { humanise, relativeTime } from '../../lib/format';
import { Dialog, DialogActions } from '../assets/components/Dialog';
import { useAnomalyLive } from '../analytics/anomaly/anomalyEvents';
import { LAYER_NAME } from '../analytics/anomaly/format';
import { ExclusionsTab, useAnomalyExclusions } from './anomaly/ExclusionsTab';
import { OverridesTab, useAnomalyOverrides } from './anomaly/OverridesTab';
import { ParamForm } from './anomaly/ParamForm';
import '../analytics/anomaly.css';
import { Notice } from '../../components/Notice';

/** Which checks run, how sensitive they are, and what they ignore
 *  (doc 31 section 9).
 *
 *  Detector policy is estate policy: a parameter moves the finding on every
 *  device the detector covers, so editing a detector is an administrator's
 *  act (doc 30 section 2). Overrides and exclusions are per device and per
 *  window, an operator's. Everyone can read all of it - a reader who cannot
 *  see why a detector is quiet will assume it is broken. */

type Tab = 'detectors' | 'overrides' | 'exclusions';
const TABS: { key: Tab; label: string }[] = [
  { key: 'detectors', label: 'Detectors' },
  { key: 'overrides', label: 'Overrides' },
  { key: 'exclusions', label: 'Exclusions' },
];
const LAYERS: DetectorLayer[] = ['L0', 'L2', 'L3', 'L4'];
const L5 = 'L5';

export function AnomalyDetectors() {
  const [params, setParams] = useSearchParams();
  const raw = params.get('tab');
  const tab: Tab = raw === 'overrides' || raw === 'exclusions' ? raw : 'detectors';
  const setTab = (t: Tab) => {
    const p = new URLSearchParams();
    if (t !== 'detectors') p.set('tab', t);
    setParams(p, { replace: true });
  };

  const health = useQuery({ queryKey: ['anomaly-health'], queryFn: api.anomalyHealth,
                            refetchInterval: 60_000 });
  const det = useQuery({ queryKey: ['anomaly-detectors'], queryFn: api.anomalyDetectors });
  const overrides = useAnomalyOverrides();
  const exclusions = useAnomalyExclusions();
  // Open-finding counts move with the sweep; the frames say when.
  useAnomalyLive(() => [['anomaly-detectors'], ['anomaly-health']]);

  const detectors = useMemo(() => det.data?.items ?? [], [det.data]);
  const counts: Record<Tab, number | undefined> = {
    detectors: det.data ? detectors.length : undefined,
    overrides: overrides.data ? (overrides.data.total ?? overrides.data.items.length) : undefined,
    exclusions: exclusions.data ? (exclusions.data.total ?? exclusions.data.items.length) : undefined,
  };

  return (
    <>
      <h2>Anomaly detectors</h2>
      <p className="subtitle">Which checks run, how sensitive they are, and what they ignore.</p>

      <div className="anom-heads">
        {health.data && <HealthLines h={health.data} />}
        {health.isError && (
          <p className="anom-health">Could not read the sweep’s health: {(health.error as Error).message}</p>
        )}
      </div>

      <div className="seg anom-tabs" role="tablist" aria-label="Anomaly settings">
        {TABS.map((t) => (
          <button key={t.key} type="button" role="tab" id={`anom-tab-${t.key}`}
                  aria-selected={tab === t.key} aria-controls={`anom-panel-${t.key}`}
                  tabIndex={tab === t.key ? 0 : -1}
                  onClick={() => setTab(t.key)}
                  onKeyDown={(e) => {
                    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
                    const i = TABS.findIndex((x) => x.key === tab);
                    const n = TABS[(i + (e.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length];
                    setTab(n.key);
                    document.getElementById(`anom-tab-${n.key}`)?.focus();
                  }}>
            {t.label}{counts[t.key] !== undefined && <b>{counts[t.key]}</b>}
          </button>
        ))}
      </div>

      <div role="tabpanel" id={`anom-panel-${tab}`} aria-labelledby={`anom-tab-${tab}`}>
        {det.isError && (
          <Notice tone="error">Could not load the detectors: {(det.error as Error).message}</Notice>
        )}
        {tab === 'detectors' && (det.isLoading
          ? <p className="muted">Loading…</p>
          : <DetectorsTab detectors={detectors} />)}
        {tab === 'overrides' && <OverridesTab detectors={detectors} />}
        {tab === 'exclusions' && <ExclusionsTab detectors={detectors} />}
      </div>
    </>
  );
}

/* ------------------------------------------------------------- health */

function HealthLines({ h }: { h: AnomalyHealth }) {
  const admin = atLeast('admin');
  if (!h.enabled) {
    return (
      <>
        <p className="anom-health"><b>Anomaly detection is off on this platform.</b>{' '}
          {admin
            ? 'Set DCIM_ANOMALY_ENABLED=true and restart the ingest workers. Findings appear after the first sweep.'
            : 'An administrator can turn it on.'}
        </p>
        <p className="anom-health">
          The catalogue below can be prepared now · derived alerts capped at {h.severity_cap}
          {' · '}explainer {h.explainer}
        </p>
      </>
    );
  }
  const age = h.last_sweep_at ? (Date.now() - new Date(h.last_sweep_at).getTime()) / 1000 : null;
  const stale = age === null || age > 3 * h.sweep_s;
  const sweep = h.detail.sweep;
  const overBudget = h.budget_per_hour != null && h.findings_per_hour > h.budget_per_hour;
  return (
    <>
      <p className="anom-health">
        {h.last_sweep_at && age !== null && age <= 600 ? (
          <>
            Sweep {relativeTime(h.last_sweep_at)}
            {h.last_sweep_ms != null && <> · {h.last_sweep_ms} ms</>}
            {h.lock_holder && <> · {h.lock_holder}</>}
          </>
        ) : <span className="warn">No sweep in the last 10 min</span>}
        {' · '}{h.findings_per_hour} findings/h
        {h.budget_per_hour != null && <> (budget {h.budget_per_hour}/h)</>}
        {overBudget && <span className="warn"> · over budget — tune the noisiest detector</span>}
        {' · '}baselines {h.baseline_last_fit_at ? `fitted ${relativeTime(h.baseline_last_fit_at)}` : 'not fitted yet'}
        {sweep.errors_1h > 0 && (
          <span className="warn" title={sweep.last_error ?? undefined}>
            {' · '}{sweep.errors_1h} sweep error{sweep.errors_1h === 1 ? '' : 's'} in the last hour
          </span>
        )}
      </p>
      {stale && h.last_sweep_at && age !== null && age <= 600 && (
        <p className="anom-health warn">
          The detector sweep last ran {relativeTime(h.last_sweep_at)}. New findings are delayed.
        </p>
      )}
      <p className="anom-health">
        Derived alerts capped at <b>{h.severity_cap}</b>
        {' · '}MAJOR escalation <b>{h.l5_major_enabled ? 'on' : 'off'}</b>
        {' · '}explainer {h.explainer}
      </p>
      <p className="anom-health">
        {h.detail.findings.by_kind.sensor} open sensor finding{h.detail.findings.by_kind.sensor === 1 ? '' : 's'}
        {' '}— no screen lists them yet; the triage view arrives in a later phase.
      </p>
    </>
  );
}

/* ------------------------------------------------------------- detectors */

type Sort = 'catalogue' | 'open' | 'judged';

const layerNum = (l: string) => Number(l.replace('L', '')) || 0;
const judged = (d: AnomalyDetector) => d.confirmed + d.dismissed;

function DetectorsTab({ detectors }: { detectors: AnomalyDetector[] }) {
  const admin = atLeast('admin');
  const [layers, setLayers] = useState<DetectorLayer[]>([]);
  const [type, setType] = useState('');
  const [text, setText] = useState('');
  const [sort, setSort] = useState<Sort>('catalogue');
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [disabling, setDisabling] = useState<AnomalyDetector | null>(null);

  const perDevice = detectors.filter((d) => d.id !== L5);
  const correlator = detectors.find((d) => d.id === L5);
  const types = useMemo(() => [...new Set(perDevice.flatMap((d) => d.device_types))].sort(),
                        [perDevice]);
  const layerCounts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const d of perDevice) c[d.layer] = (c[d.layer] ?? 0) + 1;
    return c;
  }, [perDevice]);

  const rows = useMemo(() => {
    const t = text.trim().toLowerCase();
    const out = perDevice.filter((d) =>
      (!layers.length || layers.includes(d.layer))
      && (!type || d.device_types.includes(type))
      && (!t || `${d.id} ${d.name}`.toLowerCase().includes(t)));
    out.sort((a, b) => {
      if (sort === 'open' && b.open_findings !== a.open_findings) return b.open_findings - a.open_findings;
      if (sort === 'judged' && judged(b) !== judged(a)) return judged(b) - judged(a);
      return layerNum(a.layer) - layerNum(b.layer)
        || a.id.localeCompare(b.id, undefined, { numeric: true });
    });
    return out;
  }, [perDevice, layers, type, text, sort]);
  const paged = usePaged(rows, { noun: 'detectors', always: true });

  const toggleOpen = (id: string) => setOpen((s) => {
    const n = new Set(s);
    if (n.has(id)) n.delete(id); else n.add(id);
    return n;
  });

  const qc = useQueryClient();
  const enable = useMutation({
    mutationFn: ({ id, on }: { id: string; on: boolean }) => api.editAnomalyDetector(id, { enabled: on }),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['anomaly-detectors'] });
      qc.invalidateQueries({ queryKey: ['anomaly-health'] });
    },
    onSuccess: () => setDisabling(null),
  });
  const onToggle = (d: AnomalyDetector) => {
    if (d.enabled) setDisabling(d);
    else enable.mutate({ id: d.id, on: true });
  };

  const sortHead = (key: Sort, label: string) => (
    <th className="num" aria-sort={sort === key ? 'descending' : 'none'}>
      <button type="button" className="anom-sort"
              onClick={() => setSort(sort === key ? 'catalogue' : key)}
              title={sort === key ? 'Back to catalogue order' : `Sort by ${label.toLowerCase()}, most first`}>
        {label}{sort === key ? ' ↓' : ''}
      </button>
    </th>
  );

  const row = (d: AnomalyDetector, cols: 'full' | 'l5') => {
    const expanded = open.has(d.id);
    const panelId = `anom-det-${d.id}`;
    const n = judged(d);
    return (
      <Fragment key={d.id}>
        <tr className={d.enabled ? undefined : 'is-off'}>
          <td>
            <button type="button" className="anom-expander" aria-expanded={expanded}
                    aria-controls={panelId} onClick={() => toggleOpen(d.id)}
                    aria-label={`${d.id} ${d.name} parameters`}>
              <span className="chev" aria-hidden="true">›</span>
              <span className="anom-id">{d.id}</span>
            </button>
          </td>
          <td>{d.name}</td>
          {cols === 'full' && (
            <>
              <td><span className="anom-tag" title={LAYER_NAME[d.layer]}>{d.layer}</span></td>
              <td className="anom-hide-narrow">
                {d.device_types.map(humanise).join(', ') || '—'}
                {d.devices_in_scope != null && <span className="muted"> ({d.devices_in_scope})</span>}
              </td>
              <td>
                {d.lane === 'alert' ? (
                  <span title={d.alarm_type ?? undefined}>
                    Alert {d.severity ? <StatusChip status={d.severity} /> : null}
                  </span>
                ) : <span className="chip">Advisory</span>}
              </td>
            </>
          )}
          <td className="num">v{d.version}</td>
          {cols === 'full' && (
            <>
              <td className="num">{d.open_findings}</td>
              <td className="num anom-hide-narrow"
                  title={n ? `${d.confirmed} confirmed, ${d.dismissed} dismissed at this version` : 'No verdicts yet'}>
                {d.precision == null || n < 5 ? '—' : `${d.confirmed} of ${n}`}
              </td>
            </>
          )}
          <td>
            <input type="checkbox" checked={d.enabled} disabled={!admin || enable.isPending}
                   aria-label={`${d.id} is ${d.enabled ? 'on' : 'off'}`}
                   onChange={() => onToggle(d)} />
          </td>
        </tr>
        {expanded && (
          <tr className="anom-expansion">
            <td colSpan={cols === 'full' ? 9 : 4} id={panelId}>
              <ParamForm key={`${d.id}-${d.version}-${d.updated_at ?? ''}`} detector={d} canEdit={admin} />
              {cols === 'full' && d.required_metrics.length > 0 && (
                <p className="anom-requires">
                  Requires {d.required_metrics.map((m, i) => (
                    <Fragment key={m}>{i ? ', ' : ''}<code>{m}</code></Fragment>
                  ))}
                </p>
              )}
              {cols === 'full' && d.inactive_devices != null && (
                <p className="anom-requires">
                  Inactive on {d.inactive_devices} device{d.inactive_devices === 1 ? '' : 's'} (missing points)
                  {d.cadence_s ? ` · runs every ${d.cadence_s} s` : ''}
                  {d.updated_by ? ` · last changed by ${d.updated_by} ${relativeTime(d.updated_at)}` : ''}
                </p>
              )}
              {d.baseline && (
                <p className="anom-requires">
                  Baseline fitted {relativeTime(d.baseline.fitted_at)}, {d.baseline.days} days,
                  n = {d.baseline.n} h{d.baseline.refused ? ` · ${d.baseline.refused} series refused (thin buckets)` : ''}
                </p>
              )}
              {cols === 'l5' && (
                <p className="anom-requires">
                  These are switches, not detection maths: saving never starts a new version.
                </p>
              )}
            </td>
          </tr>
        )}
      </Fragment>
    );
  };

  const allLayers = layers.length === 0;
  return (
    <>
      {!admin && <p className="anom-note" style={{ marginTop: 0 }}>Changing detectors needs an administrator.</p>}
      <div className="anom-filters">
        <div className="seg facet-seg" role="group" aria-label="Layer filter">
          <button type="button" className={allLayers ? 'active' : ''} aria-pressed={allLayers}
                  onClick={() => setLayers([])}>All</button>
          {LAYERS.filter((l) => layerCounts[l]).map((l) => {
            const pressed = layers.includes(l);
            return (
              <button key={l} type="button" className={pressed ? 'active' : ''} aria-pressed={pressed}
                      title={LAYER_NAME[l]}
                      onClick={() => setLayers(pressed ? layers.filter((x) => x !== l) : [...layers, l])}>
                {l}<b>{layerCounts[l]}</b>
              </button>
            );
          })}
        </div>
        <select value={type} onChange={(e) => setType(e.target.value)} aria-label="Device type">
          <option value="">All device types</option>
          {types.map((t) => <option key={t} value={t}>{humanise(t)}</option>)}
        </select>
        <input type="search" value={text} onChange={(e) => setText(e.target.value)}
               placeholder="Search ID or name" aria-label="Search detectors" />
      </div>

      <div className="asset-scroll">
        <table className="anom-table">
          <thead>
            <tr>
              <th>ID</th><th>Name</th><th>Layer</th><th className="anom-hide-narrow">Applies to</th>
              <th>Lane</th><th className="num">Ver</th>
              {sortHead('open', 'Open')}
              {sortHead('judged', 'Judged')}
              <th>Enabled</th>
            </tr>
          </thead>
          <tbody>
            {paged.rows.map((d) => row(d, 'full'))}
            {rows.length === 0 && (
              <tr><td colSpan={9} className="muted">
                {perDevice.length ? 'No detector matches these filters.' : 'The catalogue is empty.'}
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
      {paged.foot}

      {correlator && (
        <>
          <h3 className="anom-group-head">Correlator</h3>
          <p className="anom-note" style={{ margin: '0 0 8px' }}>
            Groups findings under one root cause. Its MAJOR switch is the only way a derived alarm
            reads above the severity cap.
          </p>
          <div className="asset-scroll">
            <table className="anom-table">
              <thead>
                <tr><th>ID</th><th>Name</th><th className="num">Ver</th><th>Enabled</th></tr>
              </thead>
              <tbody>{row(correlator, 'l5')}</tbody>
            </table>
          </div>
        </>
      )}

      {disabling && (
        <Dialog title={`Disable ${disabling.id}?`} onClose={() => setDisabling(null)}>
          <p className="anom-dialog-copy">
            Its {disabling.open_findings} open finding{disabling.open_findings === 1 ? ' is' : 's are'} resolved
            as detector disabled, and their derived alerts clear. Nothing new is raised until you
            turn it back on.
          </p>
          {enable.error && <Notice tone="error">{(enable.error as Error).message}</Notice>}
          <DialogActions>
            <span style={{ flex: 1 }} />
            <button type="button" onClick={() => setDisabling(null)}>Cancel</button>
            <button type="button" className="danger" disabled={enable.isPending}
                    onClick={() => enable.mutate({ id: disabling.id, on: false })}>
              {enable.isPending ? 'Disabling…' : `Disable ${disabling.id}`}
            </button>
          </DialogActions>
        </Dialog>
      )}
    </>
  );
}
