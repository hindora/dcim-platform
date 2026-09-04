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
import { oneLine, relativeTime } from '../../lib/format';
import { Tip } from '../../components/HoverTip';

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
          <Tip tip={oneLine(`A collector's identity, this platform's address and
                  its token stay in the file on its own host. Breaking the path
                  to the control plane from the control plane is not a repair
                  anybody can do from here.`)}>
            Which planes each collector runs, how hard it polls, and where it
            listens.
          </Tip>
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
    return <Tip className="warn" tip={row.config_error}>failed to apply</Tip>;
  }
  if (row.restart_pending) {
    return (
      <Tip className="warn" tip={oneLine(`Saved, and waiting for a restart:
        adapters read their concurrency, timeouts and ports once, when they are
        built.`)}>
        restart pending
      </Tip>
    );
  }
  if (row.version === 0) return <span className="muted">file only</span>;
  if (row.running_version !== row.version) {
    return (
      <Tip className="muted"
           tip={<>stored <b>v{row.version}</b>, running <b>v{row.running_version}</b></>}>
        v{row.version} · not fetched yet
      </Tip>
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
    // Only what somebody actually set: the stored overrides plus this
    // session's edits. Sending every field would pin all of them at whatever
    // the collector happens to run today, and a default that improves in a
    // release would then never reach it again.
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
              <Tip tip={oneLine(`Fields you do not change stay inherited from
                      the collector's own file, so a default that improves in a
                      release still reaches it.`)}>
                {row.hostname ? `${row.hostname} · ` : ''}
                Every field shows the value in force.
              </Tip>
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
            /* fieldset rather than a section with a heading: these ARE
               groups of related controls, the legend sits on the border where
               a group label belongs, and a screen reader announces which group
               each field is in without being told twice. */
            <fieldset key={section.key} className="proto">
              <legend>
                {section.title}
                {section.danger && <span className="muted"> · listener</span>}
              </legend>
              <div className="form-grid">
                {section.fields.map((f) => (
                  <Field key={f.key} field={f}
                         value={values[section.key]?.[f.key]}
                         effective={row.effective?.[section.key]?.[f.key]}
                         onChange={(v) => set(section.key, f.key, v)} />
                ))}
              </div>
            </fieldset>
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

/** One setting, holding the value that is actually in force.
 *
 *  The control carries the real number or state - not a placeholder behind an
 *  empty box, which is what the first two versions of this form did and which
 *  told an operator nothing about the collector in front of them.
 *
 *  What is NOT visible is that an untouched field stays inherited. The form
 *  shows 48 because the collector runs 48; it stores an override only for the
 *  fields somebody actually changed, so a default that improves in a release
 *  still reaches every collector that never had an opinion about it. Showing a
 *  value and pinning a value are different acts, and only the second one is a
 *  decision. */
function Field({ field, value, effective, onChange }: {
  field: ConfigField;
  value: unknown;
  effective: unknown;
  onChange: (v: unknown) => void;
}) {
  // The value in force: what an operator has typed here this session, else
  // the override already stored, else what the collector reports running.
  const current = value !== undefined ? value : effective;
  const known = current !== undefined && current !== null;

  // When a setting takes effect, and why it is bounded where it is. Both were
  // chips and a paragraph on screen; both are quieter than the value itself
  // and belong behind the field rather than above it.
  const tip = [
    field.when === 'live'
      ? 'Applies without a restart.'
      : 'Stored now; in force when the collector next starts.',
    field.detail,
  ].filter(Boolean).join(' ');

  if (field.kind === 'bool') {
    return (
      <label>
        <span>{field.label}</span>
        <select value={known ? String(current) : 'false'}
                onChange={(e) => onChange(e.target.value === 'true')}>
          <option value="true">On</option>
          <option value="false">Off</option>
        </select>
        <em className="hint">
          {tip
            ? <Tip tip={oneLine(tip)}>{field.help}<span className="why"> ?</span></Tip>
            : field.help}
        </em>
      </label>
    );
  }

  return (
    <label>
      <span>{field.label}</span>
      <input value={known ? String(current) : ''}
             inputMode={field.kind === 'int' || field.kind === 'seconds'
               ? 'numeric' : 'text'}
             onChange={(e) => onChange(
               e.target.value === '' ? undefined
                 : field.kind === 'int' || field.kind === 'seconds'
                   ? Number(e.target.value) : e.target.value)} />
      <em className="hint">
        {tip
          ? <Tip tip={oneLine(tip)}>{field.help}<span className="why"> ?</span></Tip>
          : field.help}
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
