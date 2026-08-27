import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ApiError,
  api,
  type CollectorRow,
  type CollectorsPage,
  type ConfigField,
  type ConfigSection,
} from '../../api/client';
import { relativeTime } from '../../lib/format';

/** What each collector is running, and what it has been told to run.
 *
 *  Two different questions, deliberately shown as two: the process reports the
 *  configuration version it is actually on in its heartbeat, and most settings
 *  are read once when its adapters are built. A page that showed only what was
 *  saved would report every change as though it had reached the wire. */
export function Collectors() {
  const page = useQuery<CollectorsPage>({
    queryKey: ['collectors'],
    queryFn: () => api.collectors(),
    refetchInterval: 15_000,
  });

  const [open, setOpen] = useState<string | null>(null);

  if (page.isLoading) return <p className="muted">Loading…</p>;
  if (page.error) return <div className="banner">Could not load collectors.</div>;

  const rows = page.data?.collectors ?? [];
  const sections = page.data?.schema.sections ?? [];

  return (
    <div className="stack">
      <div>
        <h2>Collectors</h2>
        <p className="subtitle">
          Which protocol planes each collector runs, how hard it polls them, and
          where its inbound listeners sit. Its identity, this platform's address
          and its token stay in the file on the collector host — breaking the
          path to the control plane from the control plane is not a repair
          anybody can do from here.
        </p>
      </div>

      <table>
        <thead>
          <tr>
            <th>Collector</th><th>Host</th><th>Build</th>
            <th className="num">Endpoints</th><th>Config</th>
            <th>Last heartbeat</th><th />
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.id}>
              <td className="mono">{c.id}</td>
              <td className="muted">{c.hostname ?? '—'}</td>
              <td className="muted mono">{c.build ?? '—'}</td>
              <td className="num">
                {c.endpoints_online.toLocaleString()}
                <span className="muted"> / {c.endpoints_owned.toLocaleString()}</span>
              </td>
              <td><ConfigState row={c} /></td>
              <td className="muted">
                {c.alive ? relativeTime(c.last_heartbeat) : (
                  <span className="warn">
                    silent · {relativeTime(c.last_heartbeat)}
                  </span>
                )}
              </td>
              <td>
                <button onClick={() => setOpen(c.id)}>Configure</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && (
        <p className="muted">
          No collector has checked in yet. One appears here on its first
          heartbeat.
        </p>
      )}

      {open && (
        <ConfigSheet
          row={rows.find((c) => c.id === open)!}
          sections={sections}
          onClose={() => setOpen(null)}
        />
      )}
    </div>
  );
}

/** Stored version against running version, which is the only honest summary.
 *
 *  `v4 · running v4` means the collector has it. `v5 · running v4` means it has
 *  not fetched yet, or fetched something it cannot apply without a restart. */
function ConfigState({ row }: { row: CollectorRow }) {
  if (row.config_error) {
    return <span className="warn" title={row.config_error}>failed to apply</span>;
  }
  if (row.restart_pending) {
    return (
      <span className="warn" title="Saved, and waiting for a restart: adapters
        read their concurrency, timeouts and ports once, when they are built.">
        restart pending
      </span>
    );
  }
  if (row.version === 0) return <span className="muted">file only</span>;
  if (row.running_version !== row.version) {
    return (
      <span className="muted" title={`stored v${row.version}, `
        + `running v${row.running_version}`}>
        v{row.version} · not fetched yet
      </span>
    );
  }
  return <span className="muted">v{row.version} · in force</span>;
}

type Values = Record<string, Record<string, unknown>>;

function ConfigSheet({ row, sections, onClose }: {
  row: CollectorRow;
  sections: ConfigSection[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [values, setValues] = useState<Values>(() => structured(row.config));
  const [confirmed, setConfirmed] = useState(false);

  const save = useMutation({
    mutationFn: () => api.setCollectorConfig(row.id, prune(values)),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['collectors'] });
      onClose();
    },
  });

  // Which listeners this edit moves. Not a validation problem - both values are
  // legal - but every device is still sending to the old address, and nothing
  // anywhere reports that as an error. Silence is the failure mode, so the
  // warning has to come before the save rather than after it.
  const moved = useMemo(
    // Against the RUNNING config, not the stored overrides: an inherited
    // listener is stored nowhere, and showing its move as "unset -> 0.0.0.0:162"
    // hides the address every device is sending to right now.
    () => listenerMoves(sections, row.effective ?? {}, values),
    [sections, row.effective, values],
  );
  const blocked = moved.length > 0 && !confirmed;

  const set = (section: string, field: string, value: unknown) =>
    setValues((v) => ({ ...v, [section]: { ...(v[section] ?? {}), [field]: value } }));

  return (
    <div className="sheet-scrim" role="dialog" aria-modal="true"
         aria-label={`Configure ${row.id}`}
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="sheet narrow">
        <header className="sheet-head">
          <div>
            <h2>{row.id}</h2>
            <p>
              {row.hostname ? `${row.hostname} · ` : ''}
              Every field shows the value in force. An empty one is inherited
              from the collector's own file — which is how a default that
              changes in a release reaches every collector that never overrode
              it. Clear a field to go back to inheriting.
            </p>
          </div>
          <button className="close" onClick={onClose} aria-label="Close">✕</button>
        </header>

        <div className="sheet-body">
          {save.error && (
            <div className="banner">
              {save.error instanceof ApiError && save.error.status === 403
                ? 'Changing what a collector runs needs an admin account.'
                : String((save.error as Error).message)}
            </div>
          )}
          {!row.effective || Object.keys(row.effective).length === 0 ? (
            <div className="banner soft">
              This collector has not reported its own settings yet, so the
              fields below show only what is overridden here. They fill in on
              its next heartbeat.
            </div>
          ) : null}
          {row.config_error && (
            <div className="banner">
              The collector could not apply its last configuration:{' '}
              <span className="mono">{row.config_error}</span>
            </div>
          )}
          {moved.length > 0 && (
            <div className="banner soft">
              <p style={{ margin: '0 0 8px' }}>
                This moves {moved.length === 1 ? 'a listener' : 'listeners'}:{' '}
                {moved.map((m) => `${m.label} ${m.from || 'unset'} → ${m.to}`)
                  .join('; ')}.
                {' '}Every device is still sending to the old address. Nothing
                reports that as an error — the symptom is silence — so they have
                to be reconfigured to match.
              </p>
              <label className="check">
                <input type="checkbox" checked={confirmed}
                       onChange={(e) => setConfirmed(e.target.checked)} />
                <span>I will reconfigure the devices to send to the new address</span>
              </label>
            </div>
          )}

          {sections.map((section) => (
            <section key={section.key} style={{ marginBottom: 26 }}>
              <h3>
                {section.title}
                {section.danger && <span className="muted"> · listener</span>}
              </h3>
              <div className="form-grid">
                {section.fields.map((f) => (
                  <Field key={f.key} field={f}
                         value={values[section.key]?.[f.key]}
                         effective={row.effective?.[section.key]?.[f.key]}
                         onChange={(v) => set(section.key, f.key, v)} />
                ))}
              </div>
            </section>
          ))}
        </div>

        <div className="sheet-foot">
          <span className="muted">
            {blocked
              ? 'Confirm the listener move to continue'
              : 'Live settings apply in seconds; the rest at the next restart'}
          </span>
          <span className="spacer" />
          <button onClick={onClose}>Cancel</button>
          <button className="primary" disabled={blocked || save.isPending}
                  onClick={() => save.mutate()}>
            {save.isPending ? 'Saving…' : 'Save'}
          </button>
        </div>
      </section>
    </div>
  );
}

/** One setting, showing the value that is actually in force.
 *
 *  An empty box beside the words "collector's file" told an operator nothing:
 *  the override document is empty on every fresh install, so every field read
 *  the same and none of them said what the collector was doing. The running
 *  value is shown instead - as the entry itself where it is overridden here,
 *  and as the placeholder or the inherit option where it comes from the
 *  collector's own file. Clearing the box goes back to inheriting. */
function Field({ field, value, effective, onChange }: {
  field: ConfigField;
  value: unknown;
  effective: unknown;
  onChange: (v: unknown) => void;
}) {
  const when = field.when === 'live'
    ? <span className="tag live">applies live</span>
    : <span className="tag">on restart</span>;
  const overridden = value !== undefined && value !== '';
  const known = effective !== undefined && effective !== null;
  const shown = known ? String(effective) : 'not reported';

  const source = overridden
    ? <span className="tag set">set here</span>
    : known
      ? <span className="tag from">from the collector</span>
      : null;

  if (field.kind === 'bool') {
    // Three states, not two: on, off, and inherit. A checkbox cannot express
    // the third, and defaulting it to off would silently disable planes nobody
    // touched. The inherit option carries what it resolves to, so the reader
    // never has to guess what inheriting means here.
    return (
      <label>
        <span>{field.label} {when} {source}</span>
        <select value={value === undefined ? '' : String(value)}
                onChange={(e) => onChange(
                  e.target.value === '' ? undefined : e.target.value === 'true')}>
          <option value="">
            {known ? `Inherit \u2014 ${effective ? 'On' : 'Off'}` : 'Inherit'}
          </option>
          <option value="true">On</option>
          <option value="false">Off</option>
        </select>
        <em className="hint">{field.help}</em>
      </label>
    );
  }

  return (
    <label>
      <span>{field.label} {when} {source}</span>
      <input value={value === undefined || value === null ? '' : String(value)}
             inputMode={field.kind === 'int' || field.kind === 'seconds'
               ? 'numeric' : 'text'}
             // The running value, so an empty box is still informative: it
             // shows what will be used if nothing is typed.
             placeholder={shown}
             onChange={(e) => onChange(
               e.target.value === '' ? undefined
                 : field.kind === 'int' || field.kind === 'seconds'
                   ? Number(e.target.value) : e.target.value)} />
      <em className="hint">
        {overridden && known && String(effective) !== String(value) ? (
          <strong>Running: {shown}{field.kind === 'seconds' ? 's' : ''}. </strong>
        ) : null}
        {field.help}
        {field.kind === 'seconds' && ' Seconds.'}
      </em>
    </label>
  );
}

function structured(config: Record<string, Record<string, unknown>>): Values {
  return JSON.parse(JSON.stringify(config ?? {}));
}

/** Drop the fields left empty, so the document says "the file decides" rather
 *  than storing a blank. */
function prune(values: Values): Values {
  const out: Values = {};
  for (const [section, fields] of Object.entries(values)) {
    const kept: Record<string, unknown> = {};
    for (const [key, v] of Object.entries(fields)) {
      if (v === undefined || v === '' || (typeof v === 'number' && Number.isNaN(v)))
        continue;
      kept[key] = v;
    }
    if (Object.keys(kept).length) out[section] = kept;
  }
  return out;
}

/** Listener moves this edit would make, measured against what is running.
 *
 *  Both values are legal, so this is not validation - it is the one change on
 *  this page whose failure mode is silence. Every device keeps sending to the
 *  address it was told, and nothing anywhere reports that as an error. */
function listenerMoves(sections: ConfigSection[],
                       running: Record<string, Record<string, unknown>>,
                       after: Values) {
  const moves: { label: string; from: string; to: string }[] = [];
  for (const section of sections) {
    for (const f of section.fields) {
      if (f.kind !== 'listen') continue;
      const from = (running?.[section.key] ?? {})[f.key];
      const to = (after?.[section.key] ?? {})[f.key];
      if (to !== undefined && to !== '' && String(to) !== String(from ?? ''))
        moves.push({ label: `${section.title} ${f.label}`,
                     from: String(from ?? ''), to: String(to) });
    }
  }
  return moves;
}
