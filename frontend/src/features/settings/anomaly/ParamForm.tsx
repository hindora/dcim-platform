import { useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  ApiError, api,
  type AnomalyDetector, type AnomalyParamValue, type ParamSpec,
} from '../../../api/client';
import { NumberInput } from '../../../components/NumberInput';
import { Dialog, DialogActions } from '../../assets/components/Dialog';
import { boundsText, paramText, rangeError, unitText } from '../../analytics/anomaly/format';

/** The fields `param_schema` describes (doc 31 section 9.4, D9).
 *
 *  Numbers are `NumberInput`s: the box can be empty mid-edit and is coerced
 *  only on blur, so clearing 900 and typing 6 gives 6, not 0 or 0600. Bounds
 *  are checked on blur and reported under the field in words; a changed value
 *  says "changed" beside a dot, never colour alone. Shared by the detector
 *  form and the per-device override dialog, so a bound reads the same in
 *  both places. */
export function ParamFields({
  specs, values, base, onChange, errors, idPrefix, disabled = false,
}: {
  specs: ParamSpec[];
  values: Record<string, AnomalyParamValue>;
  /** What "changed" is measured against: the saved values. */
  base: Record<string, AnomalyParamValue>;
  onChange: (key: string, v: AnomalyParamValue) => void;
  /** Errors to show now: blur-checked bounds and the server's field errors. */
  errors: Record<string, string>;
  idPrefix: string;
  disabled?: boolean;
}) {
  const [touched, setTouched] = useState<Set<string>>(new Set());
  const touch = (k: string) => setTouched((t) => (t.has(k) ? t : new Set(t).add(k)));

  return (
    <div className="anom-params">
      {specs.map((spec) => {
        const id = `${idPrefix}-${spec.key}`;
        const errId = `${id}-err`;
        const v = values[spec.key];
        const saved = base[spec.key] ?? spec.default;
        const changed = v !== saved;
        const local = touched.has(spec.key) ? rangeError(spec, v) : null;
        const err = errors[spec.key] ?? local;
        const u = unitText(spec.unit);
        return (
          <div key={spec.key} className="anom-param">
            <label htmlFor={id}>{spec.label}</label>
            <span className="ctl">
              {spec.kind === 'bool' ? (
                <input id={id} type="checkbox" checked={Boolean(v)} disabled={disabled}
                       onChange={(e) => onChange(spec.key, e.target.checked)} />
              ) : spec.kind === 'enum' ? (
                <select id={id} value={String(v ?? '')} disabled={disabled}
                        onChange={(e) => onChange(spec.key, e.target.value)}>
                  {(spec.options ?? []).map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              ) : (
                <NumberInput id={id} value={typeof v === 'number' ? v : Number(saved)}
                             int={spec.kind === 'int'} fallback={Number(saved)}
                             min={spec.min} max={spec.max} step={spec.step}
                             disabled={disabled}
                             aria-invalid={Boolean(err)}
                             aria-describedby={err ? errId : undefined}
                             onChange={(n) => onChange(spec.key, n)}
                             onBlur={() => touch(spec.key)} />
              )}
              {u && <span className="u">{u}</span>}
            </span>
            <span className="meta">
              {boundsText(spec)}
              {changed && <span className="changed">changed</span>}
            </span>
            {spec.help && <p className="help">{spec.help}</p>}
            {err && <p id={errId} className="anom-err" role="alert">{err}</p>}
          </div>
        );
      })}
    </div>
  );
}

/** Values to edit: every schema key, the saved value or the default. */
export function effectiveValues(specs: ParamSpec[],
                                ...layers: (Record<string, AnomalyParamValue> | undefined)[]) {
  const out: Record<string, AnomalyParamValue> = {};
  for (const s of specs) out[s.key] = s.default;
  for (const layer of layers) {
    for (const [k, v] of Object.entries(layer ?? {})) if (k in out) out[k] = v;
  }
  return out;
}

/** Any field out of bounds, against the schema. */
export function anyInvalid(specs: ParamSpec[], values: Record<string, AnomalyParamValue>): boolean {
  return specs.some((s) => rangeError(s, values[s.key]) !== null);
}

/** A server 422 that names a field, as `{key: message}`. */
export function fieldError(e: unknown): Record<string, string> {
  if (e instanceof ApiError && e.detail && typeof e.detail.key === 'string') {
    return { [e.detail.key]: e.message };
  }
  return {};
}

/** One detector's parameter form, with Revert and Save (doc 31 section 9.4).
 *
 *  A parameter change starts a new version, and that has consequences an
 *  operator must see BEFORE they happen: running totals restart, findings
 *  whose evidence the change invalidates resolve as superseded, precision
 *  restarts. So Save is labelled with the version it creates and opens a
 *  dialog that lists every change and says so. The L5 correlator's params
 *  are switches, not detection maths: saving it never bumps the version, and
 *  only turning MAJOR escalation on asks first. */
export function ParamForm({ detector, canEdit }: { detector: AnomalyDetector; canEdit: boolean }) {
  const qc = useQueryClient();
  const isL5 = detector.id === 'L5';
  const specs = detector.param_schema;
  const saved = useMemo(() => effectiveValues(specs, detector.params), [specs, detector.params]);
  const [draft, setDraft] = useState(saved);
  const [serverErr, setServerErr] = useState<Record<string, string>>({});
  const [confirming, setConfirming] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [resetKey, setResetKey] = useState(0);

  const changes = specs.filter((s) => draft[s.key] !== saved[s.key]);
  const invalid = anyInvalid(specs, draft);
  const next = detector.version + 1;
  const untyped = Object.entries(detector.params).filter(
    ([k]) => !specs.some((s) => s.key === k));

  const save = useMutation({
    mutationFn: () => api.editAnomalyDetector(detector.id, {
      params: Object.fromEntries(changes.map((s) => [s.key, draft[s.key]])),
    }),
    onSuccess: (r) => {
      setConfirming(false);
      setServerErr({});
      setProblem(null);
      setMessage(r.changed.length === 0 ? 'Nothing changed'
        : isL5 ? 'Saved' : `Saved as v${r.version}`);
      qc.invalidateQueries({ queryKey: ['anomaly-detectors'] });
      qc.invalidateQueries({ queryKey: ['anomaly-health'] });
    },
    onError: (e) => {
      const fe = fieldError(e);
      setServerErr(fe);
      setProblem(Object.keys(fe).length ? null : `Could not save: ${(e as Error).message}`);
      if (Object.keys(fe).length) setConfirming(false);
    },
  });

  const turnsMajorOn = isL5 && draft.major_enabled === true && saved.major_enabled !== true;
  const onSave = () => {
    setMessage(null);
    if (!isL5 || turnsMajorOn) setConfirming(true);
    else save.mutate();
  };
  const specOf = (k: string) => specs.find((s) => s.key === k);

  if (!specs.length) {
    return <p className="muted">This detector has no tunable parameters.</p>;
  }

  return (
    <div>
      <ParamFields key={resetKey} specs={specs} values={draft} base={saved}
                   idPrefix={`p-${detector.id}`} disabled={!canEdit}
                   errors={serverErr}
                   onChange={(k, v) => {
                     setDraft((d) => ({ ...d, [k]: v }));
                     setServerErr((e) => { const { [k]: _drop, ...rest } = e; return rest; });
                     setMessage(null);
                   }} />
      {untyped.length > 0 && (
        <p className="anom-requires">
          Not tunable here:{' '}
          {untyped.map(([k, v]) => <code key={k}>{k} = {String(v)} </code>)}
        </p>
      )}
      {canEdit && (
        <div className="anom-param-foot">
          <span className="muted small" aria-live="polite">
            {message ?? (changes.length
              ? `${changes.length} change${changes.length === 1 ? '' : 's'} to save`
              : 'Nothing changed')}
          </span>
          <span className="spacer" />
          <button type="button" disabled={!changes.length || save.isPending}
                  onClick={() => {
                    setDraft(saved); setServerErr({}); setProblem(null);
                    setResetKey((n) => n + 1);
                  }}>
            Revert
          </button>
          <button type="button" className="primary"
                  disabled={!changes.length || invalid || save.isPending}
                  onClick={onSave}>
            {save.isPending ? 'Saving…' : isL5 ? 'Save' : `Save as v${next}`}
          </button>
        </div>
      )}
      {problem && <div className="banner" role="alert" style={{ marginTop: 10 }}>{problem}</div>}

      {confirming && (
        <Dialog title={isL5 ? `Turn on MAJOR escalation?` : `Save ${detector.id} as v${next}?`}
                onClose={() => setConfirming(false)}>
          <ul className="anom-changes">
            {changes.map((s) => (
              <li key={s.key}>
                {s.label} {paramText(specOf(s.key), saved[s.key])} → {paramText(specOf(s.key), draft[s.key])}
              </li>
            ))}
          </ul>
          {isL5 ? (
            <p className="anom-dialog-copy">
              Derived roots with confirmed impact will raise MAJOR alarms that reach the alarm
              console and Jira policy. The Phase 2 gate in doc 33 should pass first.
            </p>
          ) : (
            <>
              <p className="anom-dialog-copy">
                Saving starts version {next}. The detector re-learns its running totals, so
                slow-drift findings may take hours to reappear.
              </p>
              <p className="anom-dialog-copy">
                Open findings from v{detector.version} stay open and resolve normally. If this
                change makes their accumulated evidence meaningless — for example a new CUSUM
                k or h — they are resolved as superseded, and new findings open under v{next} if
                the condition is still there.
              </p>
              <p className="anom-dialog-copy muted">
                Precision restarts at v{next}: confirmed and dismissed counts are per version.
              </p>
            </>
          )}
          {save.isError && !Object.keys(serverErr).length && (
            <p className="anom-err" role="alert">{(save.error as Error).message}</p>
          )}
          <DialogActions>
            <span style={{ flex: 1 }} />
            <button type="button" onClick={() => setConfirming(false)}>Cancel</button>
            <button type="button" className="primary" disabled={save.isPending}
                    onClick={() => save.mutate()}>
              {save.isPending ? 'Saving…' : isL5 ? 'Turn on and save' : `Save as v${next}`}
            </button>
          </DialogActions>
        </Dialog>
      )}
    </div>
  );
}
