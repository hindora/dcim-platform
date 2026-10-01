import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ApiError,
  api,
  type FirewallMatrix,
  type PoolBody,
  type PoolDetail,
  type PoolPlane,
  type PoolRow,
  type PoolsPage,
} from '../../api/client';
import { oneLine, relativeTime } from '../../lib/format';
import { Tip } from '../../components/HoverTip';

/** Collector pools: site x plane placement (docs/26 Phase 5).
 *
 *  A pool is what a collector is placed IN and what an endpoint resolves
 *  TO - by the discovery range containing its address, unless an operator
 *  overrides it per endpoint. Everything else on this page follows from
 *  that one join: members are the collectors placed here, endpoints are
 *  the ones that resolve here, and "unassigned" is the number of those no
 *  member is healthy enough to own. The firewall matrix is derived from the
 *  same rows, which is why it can be trusted where a hand-typed one cannot. */
export function Pools() {
  const page = useQuery<PoolsPage>({
    queryKey: ['pools'],
    queryFn: () => api.pools(),
    refetchInterval: 15_000,
  });
  const [open, setOpen] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  if (page.isLoading) return <p className="muted">Loading…</p>;
  if (page.error) return <div className="banner">Could not load pools.</div>;

  const pools = page.data?.pools ?? [];
  const unpooled = page.data?.unpooled;
  const sites = page.data?.sites ?? [];
  const planes = page.data?.planes ?? [];
  const taken = new Set(pools.map((p) => `${p.datacenter_id}/${p.plane}`));

  return (
    <div className="stack">
      <div style={{ display: 'flex', justifyContent: 'space-between',
                    alignItems: 'flex-end' }}>
        <div>
          <h2>Pools</h2>
          <p className="subtitle">
            <Tip tip={oneLine(`An endpoint resolves to the pool whose site and plane
                    match the discovery range containing its address. A
                    collector placed in a pool serves that pool and nothing
                    else - not even the rest of its own site.`)}>
              Which network each collector is on, and which devices that
              makes it responsible for.
            </Tip>
          </p>
        </div>
        <button className="primary" onClick={() => setCreating(true)}>
          Add pool
        </button>
      </div>

      {unpooled && unpooled.endpoints > 0 && (
        <div className="banner soft">
          {unpooled.endpoints} endpoint{unpooled.endpoints === 1 ? '' : 's'} resolve
          to no pool
          {unpooled.unassigned > 0 && (
            <> — {unpooled.unassigned} of them owned by nobody</>
          )}.
          They are served by collectors with no pool placement, exactly as
          before pools existed. Add a pool for their site and plane to bring
          them under one.
        </div>
      )}

      <table>
        <thead>
          <tr>
            <th>Pool</th><th>Site</th><th>Plane</th>
            <th className="num">
              <Tip tip="Healthy and accepting work / placed in this pool">Members</Tip>
            </th>
            <th className="num">Endpoints</th>
            <th className="num">
              <Tip tip="Endpoints resolving here that no member can own right now">
                Unassigned
              </Tip>
            </th>
            <th className="num">
              <Tip tip={oneLine(`Points/s measured from this pool's endpoints, and the
                      share of its rate budget that is. 85% of budget warns.`)}>
                Load
              </Tip>
            </th>
            <th className="num">
              <Tip tip="Poll-worker busy % of its busiest live member. 85% warns.">
                Busiest
              </Tip>
            </th>
            <th>Trap VIP</th><th>BBMD</th><th />
          </tr>
        </thead>
        <tbody>
          {pools.map((p) => (
            <tr key={p.id}>
              <td>{p.name}</td>
              <td className="muted">{p.site}</td>
              <td className="muted">{planeLabel(p.plane)}</td>
              <td className="num">
                {p.below_min_members ? (
                  <Tip className="warn"
                       tip={`Needs ${p.min_members} healthy members and has ${p.accepting_members}.`}>
                    {p.accepting_members} / {p.members.length}
                  </Tip>
                ) : (
                  <>{p.accepting_members} / {p.members.length}</>
                )}
              </td>
              <td className="num">
                {p.endpoints > 0 ? (
                  <Tip tip={Object.entries(p.protocols)
                    .map(([k, v]) => `${v} ${k}`).join(', ')}>
                    {p.endpoints}
                  </Tip>
                ) : <span className="muted">0</span>}
              </td>
              <td className="num">
                {p.unassigned > 0
                  ? <span className="warn">{p.unassigned}</span>
                  : <span className="muted">0</span>}
              </td>
              <td className="num">
                {p.points_per_s == null ? <span className="muted">—</span> : (
                  <>
                    {p.points_per_s.toFixed(0)}/s
                    {p.budget_used_pct != null && (
                      <span className={p.budget_used_pct >= 100 ? 'critical'
                        : p.budget_used_pct >= 85 ? 'warn' : 'muted'}>
                        {' '}({p.budget_used_pct.toFixed(0)}%)
                      </span>
                    )}
                  </>
                )}
              </td>
              <td className="num">
                {p.busiest_member_pct == null ? <span className="muted">—</span> : (
                  <span className={p.busiest_member_pct >= 95 ? 'critical'
                    : p.busiest_member_pct >= 85 ? 'warn' : undefined}>
                    {p.busiest_member_pct.toFixed(0)}%
                  </span>
                )}
              </td>
              <td className="mono muted">{p.trap_vip ?? '—'}</td>
              <td className="mono muted">
                {p.bbmd_settings?.enabled ? p.bbmd_settings.bbmd : (
                  <Tip tip="Static unicast: devices are addressed directly, no broadcast domain needed.">
                    static
                  </Tip>
                )}
              </td>
              <td><button onClick={() => setOpen(p.id)}>Manage</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      {pools.length === 0 && (
        <p className="muted">
          No pools yet. Every collector is placed by site alone and serves
          every plane at it. Add one per site and plane to give each
          collector exactly its own network.
        </p>
      )}

      {open && (
        <ManageSheet row={pools.find((p) => p.id === open)!}
                     onClose={() => setOpen(null)} />
      )}
      {creating && (
        <CreateDialog sites={sites} planes={planes} taken={taken}
                      onClose={() => setCreating(false)}
                      onCreated={() => setCreating(false)} />
      )}
    </div>
  );
}

const PLANE_LABEL: Record<PoolPlane, string> = {
  it_oob: 'IT-OOB', bms: 'BMS', production: 'Production', other: 'Other',
};

function planeLabel(plane: string): string {
  return PLANE_LABEL[plane as PoolPlane] ?? plane;
}

function errorText(err: unknown, what: string): string {
  if (err instanceof ApiError) {
    if (err.status === 403) return `${what} needs an admin account.`;
    return err.message;
  }
  return String((err as Error)?.message ?? err);
}

/* ------------------------------------------------------------------ create */

function CreateDialog({ sites, planes, taken, onClose, onCreated }: {
  sites: { id: string; code: string; name: string }[];
  planes: PoolPlane[];
  taken: Set<string>;
  onClose: () => void;
  onCreated: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState('');
  const [site, setSite] = useState(sites[0]?.id ?? '');
  const [plane, setPlane] = useState<PoolPlane>(planes[0] ?? 'it_oob');

  const conflict = site && taken.has(`${site}/${plane}`);
  const create = useMutation({
    mutationFn: () => api.createPool({ name: name.trim(), datacenter_id: site, plane }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['pools'] }); onCreated(); },
  });

  return (
    <div className="sheet-scrim" role="dialog" aria-modal="true" aria-label="Add pool"
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="sheet narrow">
        <header className="sheet-head">
          <div>
            <h2>Add pool</h2>
            <p>One per site and plane. Trap VIP, BBMD and budgets are set after.</p>
          </div>
          <button className="close" onClick={onClose} aria-label="Close">✕</button>
        </header>
        <div className="sheet-body">
          {create.error && (
            <div className="banner">{errorText(create.error, 'Creating a pool')}</div>
          )}
          <div className="form-grid">
            <label>
              <span>Name</span>
              <input value={name} onChange={(e) => setName(e.target.value)}
                     placeholder="DC1/BMS" autoFocus maxLength={64} />
              <em className="hint">How it reads on this page and in alarms.</em>
            </label>
            <label>
              <span>Site</span>
              <select value={site} onChange={(e) => setSite(e.target.value)}>
                {sites.map((s) => (
                  <option key={s.id} value={s.id}>{s.code} · {s.name}</option>
                ))}
              </select>
            </label>
            <label>
              <span>Plane</span>
              <select value={plane} onChange={(e) => setPlane(e.target.value as PoolPlane)}>
                {planes.map((p) => <option key={p} value={p}>{planeLabel(p)}</option>)}
              </select>
              <em className={conflict ? 'hint bad' : 'hint'}>
                {conflict
                  ? 'This site already has a pool for that plane.'
                  : 'Must match the purpose of the discovery ranges whose devices belong here.'}
              </em>
            </label>
          </div>
        </div>
        <div className="sheet-foot">
          <span className="spacer" />
          <button onClick={onClose}>Cancel</button>
          <button className="primary"
                  disabled={!name.trim() || !site || !!conflict || create.isPending}
                  onClick={() => create.mutate()}>
            {create.isPending ? 'Creating…' : 'Create'}
          </button>
        </div>
      </section>
    </div>
  );
}

/* ------------------------------------------------------------------ manage */

interface Form {
  name: string;
  cidrs: string;
  trap_vip: string;
  bbmd_enabled: boolean;
  bbmd: string;
  ttl_s: string;
  rate_budget: string;
  min_members: string;
}

function formOf(p: PoolRow): Form {
  return {
    name: p.name,
    cidrs: (p.cidrs ?? []).join('\n'),
    trap_vip: p.trap_vip ?? '',
    bbmd_enabled: Boolean(p.bbmd_settings?.enabled),
    bbmd: p.bbmd_settings?.bbmd ?? '',
    ttl_s: String(p.bbmd_settings?.ttl_s ?? 300),
    rate_budget: p.rate_budget_points_per_s == null ? '' : String(p.rate_budget_points_per_s),
    min_members: String(p.min_members),
  };
}

/** Only what differs from the row, so a save that changed nothing sends
 *  nothing and the server neither commits nor audits. */
function toPatch(form: Form, p: PoolRow): PoolBody {
  const patch: PoolBody = {};
  if (form.name.trim() !== p.name) patch.name = form.name.trim();
  const cidrs = form.cidrs.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
  if (cidrs.join('|') !== (p.cidrs ?? []).join('|')) patch.cidrs = cidrs;
  const vip = form.trap_vip.trim() || null;
  if (vip !== (p.trap_vip ?? null)) patch.trap_vip = vip;
  const bbmd = form.bbmd_enabled
    ? { enabled: true, bbmd: form.bbmd.trim(), ttl_s: Number(form.ttl_s) || 300 }
    : (form.bbmd.trim() ? { enabled: false, bbmd: form.bbmd.trim(), ttl_s: Number(form.ttl_s) || 300 } : {});
  const current = p.bbmd_settings ?? {};
  if (JSON.stringify(bbmd) !== JSON.stringify(
      Object.keys(current).length ? { enabled: Boolean(current.enabled), ...(current.bbmd ? { bbmd: current.bbmd } : {}), ttl_s: current.ttl_s ?? 300 } : {})) {
    patch.bbmd_settings = bbmd;
  }
  const budget = form.rate_budget.trim() === '' ? null : Number(form.rate_budget);
  if (budget !== (p.rate_budget_points_per_s ?? null)) patch.rate_budget_points_per_s = budget;
  const mm = Number(form.min_members) || 1;
  if (mm !== p.min_members) patch.min_members = mm;
  return patch;
}

function ManageSheet({ row, onClose }: { row: PoolRow; onClose: () => void }) {
  const qc = useQueryClient();
  const [form, setForm] = useState<Form>(() => formOf(row));
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [copied, setCopied] = useState(false);
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm((f) => ({ ...f, [k]: v }));
  const invalidate = () => qc.invalidateQueries({ queryKey: ['pools'] });

  const detail = useQuery<PoolDetail>({
    queryKey: ['pool', row.id],
    queryFn: () => api.pool(row.id),
  });

  const patch = toPatch(form, row);
  const dirty = Object.keys(patch).length > 0;
  const save = useMutation({
    mutationFn: () => api.patchPool(row.id, patch),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: () => api.deletePool(row.id),
    onSuccess: () => { invalidate(); onClose(); },
  });
  const matrix = useMutation({
    mutationFn: () => api.poolFirewallMatrix(row.id),
  });

  const ranges = detail.data?.ranges ?? [];
  const placed = row.members.length;

  return (
    <div className="sheet-scrim" role="dialog" aria-modal="true" aria-label={row.name}
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="sheet">
        <header className="sheet-head">
          <div>
            <h2>{row.name}</h2>
            <p>
              {row.site} · {planeLabel(row.plane)} — site and plane are the pool's
              identity; to change them, create another pool.
            </p>
          </div>
          <button className="close" onClick={onClose} aria-label="Close">✕</button>
        </header>
        <div className="sheet-body">
          {row.below_min_members && (
            <div className="banner">
              Below its minimum: {row.accepting_members} of the required {row.min_members} members
              are healthy and accepting work. This is what raises the pool_below_min_members alarm.
            </div>
          )}

          <fieldset className="proto">
            <legend>Members</legend>
            {placed === 0 ? (
              <p className="muted">
                No collector is placed here yet. Place one from Settings → Collectors;
                until then this pool's endpoints are owned by nobody.
              </p>
            ) : (
              <table>
                <thead>
                  <tr><th>Collector</th><th>State</th><th>Heartbeat</th>
                      <th className="num">Endpoints</th></tr>
                </thead>
                <tbody>
                  {row.members.map((m) => (
                    <tr key={m.collector_id}>
                      <td className="mono">{m.collector_id}</td>
                      <td className="muted">
                        {m.state}{!m.healthy && <span className="warn"> · silent</span>}
                        {m.healthy && !m.accepting && <span className="muted"> · not accepting</span>}
                      </td>
                      <td className="muted">
                        {m.heartbeat_age_s == null ? 'never' : `${Math.round(m.heartbeat_age_s)}s ago`}
                      </td>
                      <td className="num">{m.endpoints_online} / {m.endpoints_owned}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </fieldset>

          <fieldset className="proto">
            <legend>Endpoints</legend>
            <p className="muted">
              {row.endpoints} resolve here
              {row.endpoints > 0 && (
                <> ({Object.entries(row.protocols).map(([k, v]) => `${v} ${k}`).join(', ')})</>
              )}
              : {row.owned} owned, {row.unassigned > 0
                ? <span className="warn">{row.unassigned} unassigned</span>
                : '0 unassigned'}.
            </p>
            {detail.isLoading ? <p className="muted">Loading ranges…</p> : ranges.length === 0 ? (
              <p className="muted">
                No discovery range at {row.site} has purpose {planeLabel(row.plane)}, so nothing
                resolves here by address. Add one under Discovery, or override endpoints one by one.
              </p>
            ) : (
              <p className="muted">
                Fed by {ranges.map((r) => (
                  <span key={r.id} className="mono">
                    {r.cidr}{!r.enabled && ' (disabled)'}{' '}
                  </span>
                ))}
              </p>
            )}
          </fieldset>

          <fieldset className="proto">
            <legend>Settings</legend>
            {save.error && (
              <div className="banner">{errorText(save.error, 'Changing a pool')}</div>
            )}
            <div className="form-grid">
              <label>
                <span>Name</span>
                <input value={form.name} onChange={(e) => set('name', e.target.value)} maxLength={64} />
              </label>
              <label>
                <span>Trap VIP</span>
                <input value={form.trap_vip} onChange={(e) => set('trap_vip', e.target.value)}
                       placeholder="10.52.1.250" className="mono" />
                <em className="hint">
                  A floating address the pool's members share for inbound traps. Empty:
                  devices send to each collector's own address.
                </em>
              </label>
              <label>
                <span>Extra CIDRs</span>
                <textarea value={form.cidrs} onChange={(e) => set('cidrs', e.target.value)}
                          rows={2} className="mono" placeholder="one per line" />
                <em className="hint">
                  Networks members must reach beyond the discovery ranges — a BBMD or
                  trap VIP network that is not itself swept. Listed in the firewall matrix.
                </em>
              </label>
              <label>
                <span>BACnet discovery</span>
                <select value={form.bbmd_enabled ? 'fdr' : 'static'}
                        onChange={(e) => set('bbmd_enabled', e.target.value === 'fdr')}>
                  <option value="static">Static unicast (default)</option>
                  <option value="fdr">Foreign Device Registration with a BBMD</option>
                </select>
                <em className="hint">
                  Static needs no broadcast domain and no facilities change beyond a pinhole.
                  FDR is for discovering devices on a subnet this pool's collectors are not on.
                </em>
              </label>
              {form.bbmd_enabled && (
                <>
                  <label>
                    <span>BBMD</span>
                    <input value={form.bbmd} onChange={(e) => set('bbmd', e.target.value)}
                           placeholder="10.52.1.1:47808" className="mono" />
                    <em className="hint">host:port. Members register here on every assignment refresh.</em>
                  </label>
                  <label>
                    <span>Registration TTL (s)</span>
                    <input value={form.ttl_s} onChange={(e) => set('ttl_s', e.target.value)}
                           inputMode="numeric" className="mono" />
                    <em className="hint">Renewed at half of this. 10–65535.</em>
                  </label>
                </>
              )}
              <label>
                <span>Rate budget (points/s)</span>
                <input value={form.rate_budget} onChange={(e) => set('rate_budget', e.target.value)}
                       inputMode="numeric" className="mono" placeholder="unset" />
                <em className="hint">
                  The load the target network agreed to take — a BMS supervisor's or
                  gateway's ceiling, not the collector's. Measured against the points/s
                  this pool's endpoints publish; alarms at 85%. Nothing throttles to it yet.
                </em>
              </label>
              <label>
                <span>Minimum members</span>
                <input value={form.min_members} onChange={(e) => set('min_members', e.target.value)}
                       inputMode="numeric" className="mono" />
                <em className="hint">
                  Above 1 turns on HA: failover when a member goes silent, and an alarm when
                  fewer than this are healthy.
                </em>
              </label>
            </div>
            <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
              <button className="primary" disabled={!dirty || save.isPending}
                      onClick={() => save.mutate()}>
                {save.isPending ? 'Saving…' : 'Save settings'}
              </button>
              {save.isSuccess && !dirty && (
                <span className="muted">Saved · reaches members on their next assignment fetch.</span>
              )}
            </div>
          </fieldset>

          <RebalancePanel row={row} onDone={invalidate} />

          <fieldset className="proto">
            <legend>Firewall matrix</legend>
            <p className="muted">
              Derived from this pool's ranges, the protocols present on its endpoints, and
              its trap VIP and BBMD — the rules a network team needs, in the words of a
              change request.
            </p>
            {matrix.error && (
              <div className="banner">{errorText(matrix.error, 'Building the matrix')}</div>
            )}
            <div style={{ display: 'flex', gap: 8 }}>
              <button disabled={matrix.isPending} onClick={() => matrix.mutate()}>
                {matrix.isPending ? 'Building…' : matrix.data ? 'Rebuild' : 'Build'}
              </button>
              {matrix.data && (
                <>
                  <button onClick={() => {
                    void navigator.clipboard.writeText(matrix.data!.text);
                    setCopied(true);
                    setTimeout(() => setCopied(false), 1500);
                  }}>{copied ? 'Copied' : 'Copy'}</button>
                  <button onClick={() => downloadText(
                    `firewall-${row.site}-${row.plane}.txt`, matrix.data!.text)}>
                    Download
                  </button>
                </>
              )}
            </div>
            {matrix.data && <MatrixTable m={matrix.data} />}
          </fieldset>

          <fieldset className="proto">
            <legend>Delete</legend>
            {remove.error && (
              <div className="banner">{errorText(remove.error, 'Deleting a pool')}</div>
            )}
            {placed > 0 ? (
              <p className="muted">
                {placed} collector{placed === 1 ? ' is' : 's are'} placed here. Move them to
                another pool, or out of any pool, first — deleting would otherwise silently
                turn them back into serves-everything collectors.
              </p>
            ) : !confirmDelete ? (
              <button onClick={() => setConfirmDelete(true)}>Delete pool</button>
            ) : (
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <span>Really delete {row.name}? Its endpoints fall back to unplaced collectors.</span>
                <button onClick={() => setConfirmDelete(false)}>Cancel</button>
                <button className="primary" disabled={remove.isPending}
                        onClick={() => remove.mutate()}>
                  {remove.isPending ? 'Deleting…' : 'Confirm'}
                </button>
              </div>
            )}
          </fieldset>

          <p className="muted">
            Created {relativeTime(row.created_at)} · updated {relativeTime(row.updated_at)}
          </p>
        </div>
      </section>
    </div>
  );
}

/** docs/26 Phase 5's "rebalance now (with preview)". The assigner damps
 *  ordinary rebalances so a flapping member cannot reshuffle a pool, which
 *  also means a member added to a nearly-even pool can sit under-used until
 *  the imbalance is real. This shows the exact move first - the assigner's
 *  own pass with this pool's damping bypassed - then makes it. */
function RebalancePanel({ row, onDone }: { row: PoolRow; onDone: () => void }) {
  const qc = useQueryClient();
  const preview = useMutation({ mutationFn: () => api.rebalancePreview(row.id) });
  const apply = useMutation({
    mutationFn: () => api.rebalancePool(row.id),
    onSuccess: () => {
      onDone();
      void qc.invalidateQueries({ queryKey: ['shard-map'] });
      preview.reset();
    },
  });
  const p = preview.data;
  const name = (id: string | null) => (id == null || id === 'None' ? 'nobody' : id);
  return (
    <fieldset className="proto">
      <legend>Rebalance</legend>
      <p className="muted" style={{ marginTop: 0 }}>
        Work moves on its own only when a member is gone, draining or failed, or when the
        pool is uneven by 10 endpoints and 2x. Preview what an even split would move now.
      </p>
      {(preview.error || apply.error) && (
        <div className="banner">{errorText(preview.error ?? apply.error, 'Rebalancing')}</div>
      )}
      {apply.data && (
        <p className="ok">
          Rebalanced: {apply.data.recorded_moves} moves recorded. Collectors pick them up on
          their next assignment fetch.
        </p>
      )}
      {p && (
        <>
          {p.frozen && (
            <div className="banner">
              A change freeze covers this pool's site. Nothing moves until it ends.
            </div>
          )}
          {p.balanced ? (
            <p className="muted">Already as even as the plan makes it — nothing would move.</p>
          ) : (
            <>
              <p>
                {p.moving} of {p.endpoints} endpoints would move
                {p.automatic > 0 && (
                  <span className="muted"> ({p.automatic} of them the next tick would move anyway)</span>
                )}.
              </p>
              <table>
                <thead><tr><th>Collector</th><th className="num">Now</th><th className="num">After</th></tr></thead>
                <tbody>
                  {[...new Set([...Object.keys(p.before), ...Object.keys(p.after)])].sort().map((k) => (
                    <tr key={k}>
                      <td className="mono">{name(k)}</td>
                      <td className="num">{p.before[k] ?? 0}</td>
                      <td className="num">{p.after[k] ?? 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </>
      )}
      <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
        <button disabled={preview.isPending} onClick={() => preview.mutate()}>
          {preview.isPending ? 'Working it out…' : p ? 'Preview again' : 'Preview'}
        </button>
        {p && !p.balanced && !p.frozen && (
          <button className="primary" disabled={apply.isPending} onClick={() => apply.mutate()}>
            {apply.isPending ? 'Rebalancing…' : `Rebalance now (${p.moving} moves)`}
          </button>
        )}
      </div>
    </fieldset>
  );
}

function MatrixTable({ m }: { m: FirewallMatrix }) {
  return (
    <div className="table-frame" style={{ marginTop: 8 }}>
      <table>
        <thead>
          <tr><th>Direction</th><th>From</th><th>To</th><th>Port</th><th>Why</th></tr>
        </thead>
        <tbody>
          {m.rows.map((r, i) => (
            <tr key={i}>
              <td className="muted">{r.direction}</td>
              <td>{r.from}</td>
              <td className="mono">{r.to}</td>
              <td className="mono">{r.transport}/{r.port}</td>
              <td className="muted">{r.why}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function downloadText(filename: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
