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
 *  saved would report every change as though it had reached the wire.
 *
 *  A THIRD question sits beside those two now that a fleet can hold more than
 *  one collector: WHERE it is placed and WHETHER it is doing anything. Those
 *  are an admin's decisions, not a heartbeat fact, and they are what a second
 *  collector needs before it can be trusted with any of a site's devices. */
export function Collectors() {
  const page = useQuery<CollectorsPage>({
    queryKey: ['collectors'],
    queryFn: () => api.collectors(),
    refetchInterval: 15_000,
  });

  const [open, setOpen] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [justCreated, setJustCreated] = useState<
    { id: string; token: string } | null>(null);

  if (page.isLoading) return <p className="muted">Loading…</p>;
  if (page.error) return <div className="banner">Could not load collectors.</div>;

  const rows = page.data?.collectors ?? [];
  const sections = page.data?.schema.sections ?? [];
  const sites = page.data?.sites ?? [];
  const unassigned = page.data?.unassigned ?? 0;

  return (
    <div className="stack">
      <div style={{ display: 'flex', justifyContent: 'space-between',
                    alignItems: 'flex-end' }}>
        <div>
          <h2>Collectors</h2>
          <p className="subtitle">
            <Tip tip={oneLine(`A collector's identity, this platform's address and
                    its token stay in the file on its own host. Breaking the path
                    to the control plane from the control plane is not a repair
                    anybody can do from here.`)}>
              Which sites and planes each collector runs, how hard it polls,
              and where it listens.
            </Tip>
          </p>
        </div>
        <button onClick={() => setCreating(true)}>Add collector</button>
      </div>

      {unassigned > 0 && (
        <div className="banner soft">
          <Tip tip={oneLine(`These endpoints hashed to no collector at all — either
                  their site has nobody placed in it, or every collector placed
                  there is draining or decommissioned. Nothing is polling them.`)}>
            {unassigned.toLocaleString()} endpoint{unassigned === 1 ? '' : 's'}{' '}
            {unassigned === 1 ? 'is' : 'are'} unassigned — no collector serves
            their site.
          </Tip>
        </div>
      )}

      <table>
        <thead>
          <tr>
            <th>Collector</th><th>Site</th><th>State</th>
            <th>Host</th><th>Build</th>
            <th className="num">Endpoints</th><th>Config</th>
            <th>Last heartbeat</th><th />
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.id}>
              <td className="mono">{c.id}</td>
              <td className="muted">{c.site ?? (
                <Tip tip="No site placed. Eligible for every site — the correct
                          default for a single-collector deployment.">any</Tip>
              )}</td>
              <td><StateBadge row={c} /></td>
              <td className="muted">{c.hostname ?? '—'}</td>
              <td className="muted mono">{c.build ?? '—'}</td>
              <td className="num">
                <EndpointCount row={c} />
              </td>
              <td><ConfigState row={c} /></td>
              <td className="muted">
                {!c.has_run ? (
                  <span className="muted">never checked in</span>
                ) : c.alive ? relativeTime(c.last_heartbeat) : (
                  <span className="warn">
                    silent · {relativeTime(c.last_heartbeat)}
                  </span>
                )}
              </td>
              <td>
                <button onClick={() => setOpen(c.id)}>Manage</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && (
        <p className="muted">
          No collector has checked in yet. One appears here on its first
          heartbeat, or add one below and it arrives already placed.
        </p>
      )}

      {open && (
        <ManageSheet
          row={rows.find((c) => c.id === open)!}
          sections={sections}
          sites={sites}
          onClose={() => setOpen(null)}
        />
      )}

      {creating && (
        <CreateDialog
          existing={new Set(rows.map((c) => c.id))}
          sites={sites}
          onClose={() => setCreating(false)}
          onCreated={(id, token) => { setCreating(false); setJustCreated({ id, token }); }}
        />
      )}

      {justCreated && (
        <TokenDialog id={justCreated.id} token={justCreated.token}
                     title="Collector created"
                     lede={<>
                       <span className="mono">{justCreated.id}</span> is placed
                       and approved. It takes no work until it first checks in
                       with this token.
                     </>}
                     onClose={() => setJustCreated(null)} />
      )}
    </div>
  );
}

/** Placement + heartbeat, in one badge. `pending` and `draining` are an
 *  admin's decision and outrank whatever the heartbeat is doing. */
function StateBadge({ row }: { row: CollectorRow }) {
  if (row.state === 'decommissioned') {
    return <Tip className="muted" tip="Retired. Its token no longer works.">
      decommissioned
    </Tip>;
  }
  if (row.state === 'pending') {
    return <Tip className="warn" tip={oneLine(`Registered itself but nobody has
            approved it yet. It takes no work — an unknown id is as often a
            typo in a config file as a new machine.`)}>
      pending approval
    </Tip>;
  }
  if (row.state === 'draining') {
    return <Tip className="warn" tip={oneLine(`Taking no new share of the hash.
            Its unpinned endpoints have moved to the rest of its site; anything
            still pinned to it stays here.`)}>
      draining
    </Tip>;
  }
  if (!row.has_run) {
    return <Tip className="muted" tip="Created but never started. It takes no
            work until its first heartbeat.">not yet run</Tip>;
  }
  return <span className="ok">active</span>;
}

/** What it owns now, against what the CURRENT plan would give it. The two
 *  differ while it is stale, mid-move, or was just approved — and that gap is
 *  exactly what an operator opens this page to check. */
function EndpointCount({ row }: { row: CollectorRow }) {
  const mismatch = row.has_run && row.state === 'active'
    && Math.abs(row.planned - row.endpoints_owned) > 0;
  return (
    <Tip tip={mismatch ? oneLine(`Owns ${row.endpoints_owned}, but the current
              plan gives it ${row.planned}. It will catch up on its next
              assignment fetch, within about thirty seconds.`) : undefined}>
      {row.endpoints_online.toLocaleString()}
      <span className="muted"> / {row.endpoints_owned.toLocaleString()}</span>
      {row.pinned > 0 && (
        <span className="muted"> · {row.pinned} pinned</span>
      )}
      {mismatch && <span className="warn"> ·</span>}
    </Tip>
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

/** Create a collector before it exists: name it, place it, and hand the
 *  operator the one-time token to put on its host. */
function CreateDialog({ existing, sites, onClose, onCreated }: {
  existing: Set<string>;
  sites: { id: string; code: string; name: string }[];
  onClose: () => void;
  onCreated: (id: string, token: string) => void;
}) {
  const [id, setId] = useState('');
  const [site, setSite] = useState('');

  const idError = id === '' ? null
    : !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(id)
      ? "Letters, digits, '-' and '_' only."
      : existing.has(id) ? 'A collector with this id already exists.' : null;

  const create = useMutation({
    mutationFn: () => api.createCollector(id, site || null),
    onSuccess: (r) => onCreated(r.id, r.token),
  });

  return (
    <div className="sheet-scrim" role="dialog" aria-modal="true"
         aria-label="Add collector"
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="sheet narrow">
        <header className="sheet-head">
          <div>
            <h2>Add collector</h2>
            <p>
              Placed and approved before it ever runs, so its first
              assignment is the right one.
            </p>
          </div>
          <button className="close" onClick={onClose} aria-label="Close">✕</button>
        </header>
        <div className="sheet-body">
          {create.error && (
            <div className="banner">
              {create.error instanceof ApiError && create.error.status === 409
                ? 'A collector with this id already exists.'
                : create.error instanceof ApiError && create.error.status === 403
                  ? 'Creating a collector needs an admin account.'
                  : String((create.error as Error).message)}
            </div>
          )}
          <div className="form-grid">
            <label>
              <span>Collector id</span>
              <input value={id} onChange={(e) => setId(e.target.value)}
                     placeholder="col-dc2-oob" autoFocus />
              <em className={idError ? 'hint bad' : 'hint'}>
                {idError ?? 'What the collector names itself as, on its host.'}
              </em>
            </label>
            <label>
              <span>Site</span>
              <select value={site} onChange={(e) => setSite(e.target.value)}>
                <option value="">Any (single-collector default)</option>
                {sites.map((s) => (
                  <option key={s.id} value={s.id}>{s.code} · {s.name}</option>
                ))}
              </select>
              <em className="hint">
                Which datacenter's endpoints it may be given. Only devices at
                this site will ever hash to it.
              </em>
            </label>
          </div>
        </div>
        <div className="sheet-foot">
          <span className="spacer" />
          <button onClick={onClose}>Cancel</button>
          <button className="primary"
                  disabled={!id || !!idError || create.isPending}
                  onClick={() => create.mutate()}>
            {create.isPending ? 'Creating…' : 'Create'}
          </button>
        </div>
      </section>
    </div>
  );
}

/** A token shown exactly once. The platform keeps no copy of it — only the
 *  generation that makes it valid — so this is the only place it is ever
 *  visible again. */
function TokenDialog({ id, token, title, lede, onClose }: {
  id: string;
  token: string;
  title: string;
  lede: React.ReactNode;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="sheet-scrim" role="dialog" aria-modal="true"
         aria-label={title}
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="sheet narrow">
        <header className="sheet-head">
          <div>
            <h2>{title}</h2>
            <p>{lede}</p>
          </div>
          <button className="close" onClick={onClose} aria-label="Close">✕</button>
        </header>
        <div className="sheet-body">
          <div className="banner soft">
            This token is shown once. Copy it into the collector's environment
            (<code>DCIM_COLLECTOR_TOKEN</code>) or config now — the platform
            keeps no copy to show again.
          </div>
          <div className="form-grid">
            <label>
              <span>Collector id</span>
              <input value={id} readOnly />
            </label>
            <label>
              <span>Token</span>
              <input value={token} readOnly className="mono" />
            </label>
          </div>
        </div>
        <div className="sheet-foot">
          <span className="spacer" />
          <button onClick={() => {
            navigator.clipboard?.writeText(token).then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 2000);
            });
          }}>
            {copied ? 'Copied' : 'Copy token'}
          </button>
          <button className="primary" onClick={onClose}>Done</button>
        </div>
      </section>
    </div>
  );
}

function ManageSheet({ row, sections, sites, onClose }: {
  row: CollectorRow;
  sections: ConfigSection[];
  sites: { id: string; code: string; name: string }[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [values, setValues] = useState<Values>(() => structured(row.config));
  const [confirmed, setConfirmed] = useState(false);
  const [site, setSite] = useState(row.datacenter_id ?? '');
  const [confirmDecommission, setConfirmDecommission] = useState(false);
  const [reissued, setReissued] = useState<{ id: string; token: string } | null>(null);

  const invalidate = () => qc.invalidateQueries({ queryKey: ['collectors'] });

  const saveConfig = useMutation({
    // Only what somebody actually set: the stored overrides plus this
    // session's edits. Sending every field would pin all of them at whatever
    // the collector happens to run today, and a default that improves in a
    // release would then never reach it again.
    mutationFn: () => api.setCollectorConfig(row.id, prune(values)),
    onSuccess: () => { invalidate(); onClose(); },
  });

  const placement = useMutation({
    mutationFn: (body: Parameters<typeof api.patchCollector>[1]) =>
      api.patchCollector(row.id, body),
    onSuccess: invalidate,
  });

  const reissue = useMutation({
    mutationFn: () => api.issueCollectorToken(row.id),
    onSuccess: (r) => { invalidate(); setReissued({ id: row.id, token: r.token }); },
  });

  const decommission = useMutation({
    mutationFn: () => api.decommissionCollector(row.id),
    onSuccess: () => { invalidate(); onClose(); },
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

  const locked = row.state === 'decommissioned';

  return (
    <>
      <div className="sheet-scrim" role="dialog" aria-modal="true"
           aria-label={`Manage ${row.id}`}
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
            {locked && (
              <div className="banner soft">
                Decommissioned {relativeTime(row.state_changed_at)}
                {row.state_changed_by ? ` by ${row.state_changed_by}` : ''}. Its
                token no longer works and it cannot be changed further.
              </div>
            )}

            <fieldset className="proto">
              <legend>Placement</legend>
              <div className="form-grid">
                <label>
                  <span>Site</span>
                  <select value={site} disabled={locked}
                          onChange={(e) => setSite(e.target.value)}>
                    <option value="">Any (single-collector default)</option>
                    {sites.map((s) => (
                      <option key={s.id} value={s.id}>{s.code} · {s.name}</option>
                    ))}
                  </select>
                  <em className="hint">
                    Only devices at this site will ever hash to it. Changing
                    it moves endpoints on the next assignment fetch.
                  </em>
                </label>
              </div>
              <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                {site !== (row.datacenter_id ?? '') && (
                  <button disabled={locked || placement.isPending}
                          onClick={() => placement.mutate({ datacenter_id: site || null })}>
                    {placement.isPending ? 'Saving…' : 'Save placement'}
                  </button>
                )}
                {row.state === 'pending' && (
                  <button className="primary" disabled={locked || placement.isPending}
                          onClick={() => placement.mutate({ state: 'active' })}>
                    Approve — start giving it work
                  </button>
                )}
                {row.state === 'active' && (
                  <button disabled={locked || placement.isPending}
                          onClick={() => placement.mutate({ state: 'draining' })}>
                    Drain — move its endpoints elsewhere
                  </button>
                )}
                {row.state === 'draining' && (
                  <button disabled={locked || placement.isPending}
                          onClick={() => placement.mutate({ state: 'active' })}>
                    Resume — take work again
                  </button>
                )}
              </div>
            </fieldset>

            <fieldset className="proto">
              <legend>Token</legend>
              <p className="muted" style={{ margin: '0 0 8px' }}>
                Generation {row.token_generation}. Issuing a new one revokes
                every token this collector currently holds within about
                fifteen seconds.
              </p>
              <button disabled={locked || reissue.isPending}
                      onClick={() => reissue.mutate()}>
                {reissue.isPending ? 'Issuing…' : 'Issue new token'}
              </button>
            </fieldset>

            {saveConfig.error && (
              <div className="banner">
                {saveConfig.error instanceof ApiError && saveConfig.error.status === 403
                  ? 'Changing what a collector runs needs an admin account.'
                  : String((saveConfig.error as Error).message)}
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
                    <Field key={f.key} field={f} disabled={locked}
                           value={values[section.key]?.[f.key]}
                           effective={row.effective?.[section.key]?.[f.key]}
                           onChange={(v) => set(section.key, f.key, v)} />
                  ))}
                </div>
              </fieldset>
            ))}

            <fieldset className="proto">
              <legend>Decommission</legend>
              <p className="muted" style={{ margin: '0 0 8px' }}>
                Retires this collector: its token stops working and anything
                pinned to it releases back to the hash. Not reversible from
                here — a machine coming back into service should be created
                as a new collector.
              </p>
              {!confirmDecommission ? (
                <button disabled={locked}
                        onClick={() => setConfirmDecommission(true)}>
                  Decommission…
                </button>
              ) : (
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  <span className="warn">Really decommission {row.id}?</span>
                  <button onClick={() => setConfirmDecommission(false)}>Cancel</button>
                  <button className="primary" disabled={decommission.isPending}
                          onClick={() => decommission.mutate()}>
                    {decommission.isPending ? 'Decommissioning…' : 'Confirm'}
                  </button>
                </div>
              )}
            </fieldset>
          </div>

          <div className="sheet-foot">
            <span className="muted">
              {blocked
                ? 'Confirm the listener move to continue'
                : 'Live settings apply in seconds; the rest at the next restart'}
            </span>
            <span className="spacer" />
            <button onClick={onClose}>Cancel</button>
            <button className="primary" disabled={locked || blocked || saveConfig.isPending}
                    onClick={() => saveConfig.mutate()}>
              {saveConfig.isPending ? 'Saving…' : 'Save config'}
            </button>
          </div>
        </section>
      </div>
      {reissued && (
        <TokenDialog id={reissued.id} token={reissued.token}
                     title="New token issued"
                     lede={<>Every token <span className="mono">{reissued.id}</span>{' '}
                       held before this is now refused.</>}
                     onClose={() => setReissued(null)} />
      )}
    </>
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
function Field({ field, value, effective, onChange, disabled }: {
  field: ConfigField;
  value: unknown;
  effective: unknown;
  onChange: (v: unknown) => void;
  disabled?: boolean;
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
        <select value={known ? String(current) : 'false'} disabled={disabled}
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
      <input value={known ? String(current) : ''} disabled={disabled}
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

type Values = Record<string, Record<string, unknown>>;

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
