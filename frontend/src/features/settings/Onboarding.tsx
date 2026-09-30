import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query';
import {
  ApiError,
  api,
  type CollectorDetail,
  type CollectorsPage,
  type EnrollmentToken,
  type FirewallMatrix,
  type PoolPlane,
  type PoolReadiness,
  type PoolRow,
  type PoolsPage,
} from '../../api/client';
import { humanise, oneLine, relativeTime, untilTime } from '../../lib/format';
import { Tip } from '../../components/HoverTip';
import { StateBadge } from './Collectors';
import { Preflight } from './CollectorDetail';

/** Site onboarding (docs/26 Phase 8): bring one network at one site under
 *  collection without SQL, a shell on the core, or a runbook.
 *
 *  One pool at a time, because that is the unit a real bring-up is done in:
 *  the IT-OOB VLAN and the BMS network have different owners, different
 *  change processes and different firewall tickets, and the second one is
 *  usually weeks behind the first. Running both through one wizard would
 *  make the slower team's ticket block the faster team's verification.
 *
 *  Nothing here is new behaviour. Every step calls an API the Pools,
 *  Collectors and Discovery pages already use; the wizard's own
 *  contribution is order, and a rail whose marks are derived from the
 *  server on every render - a step is done because the platform can see
 *  it done, never because somebody clicked Next past it. The pool and
 *  collector live in the URL, so a refresh, or handing the link to the
 *  engineer at the site, lands on the same bring-up. */
export function Onboarding() {
  const [params, setParams] = useSearchParams();
  const poolId = params.get('pool');
  const collectorId = params.get('collector');
  const step = STEPS.find((s) => s.key === params.get('step'))?.key ?? 'pool';

  const set = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v == null) next.delete(k); else next.set(k, v);
    }
    setParams(next, { replace: true });
  };

  const pools = useQuery<PoolsPage>({
    queryKey: ['pools'], queryFn: () => api.pools(), refetchInterval: 15_000,
  });
  const pool = pools.data?.pools.find((p) => p.id === poolId) ?? null;
  const readiness = useQuery<PoolReadiness>({
    queryKey: ['pool-readiness', poolId],
    queryFn: () => api.poolReadiness(poolId!),
    enabled: !!poolId,
    refetchInterval: 10_000,
  });
  const collector = useQuery<CollectorDetail>({
    queryKey: ['collector', collectorId],
    queryFn: () => api.collectorDetail(collectorId!),
    enabled: !!collectorId,
    // Fast while someone is waiting for the site engineer's enroll to land.
    refetchInterval: step === 'enroll' ? 5_000 : 15_000,
  });
  const [firewallBuilt, setFirewallBuilt] = useState(false);

  if (pools.isLoading) return <p className="muted">Loading…</p>;
  if (pools.error) return <div className="banner">Could not load pools.</div>;

  const marks = markSteps({
    pool, collector: collector.data ?? null, readiness: readiness.data ?? null,
    firewallBuilt,
  });
  const index = STEPS.findIndex((s) => s.key === step);
  const go = (key: StepKey) => set({ step: key });
  const needsPool = (key: StepKey) => key !== 'pool' && !pool;
  const needsCollector = (key: StepKey) =>
    (key === 'enroll' || key === 'traps') && !collectorId;

  return (
    <div className="stack">
      <div>
        <h2>Site onboarding</h2>
        <p className="subtitle">
          <Tip tip={oneLine(`Each mark on the left is read back from the platform,
                  not remembered from clicking Next: a step goes red again if
                  what it checked stops being true.`)}>
            Bring one network at one site under collection, from an empty pool
            to devices reporting.
          </Tip>
        </p>
      </div>

      <div className="wizard">
        <ol className="wizard-rail" aria-label="Onboarding steps">
          {STEPS.map((s, i) => {
            const m = marks[s.key];
            const blocked = needsPool(s.key) || needsCollector(s.key);
            return (
              <li key={s.key}>
                <button className={s.key === step ? 'current' : ''}
                        disabled={blocked}
                        aria-current={s.key === step ? 'step' : undefined}
                        onClick={() => go(s.key)}>
                  <span className={`mark ${m === true ? 'ok' : m === false ? 'bad' : ''}`}>
                    {m === true ? '✓' : m === false ? '!' : i + 1}
                  </span>
                  <span>{s.label}</span>
                </button>
              </li>
            );
          })}
        </ol>

        <section className="wizard-step">
          {pool && step !== 'pool' && (
            <p className="muted" style={{ margin: 0 }}>
              Pool <strong>{pool.name}</strong> · {pool.site} · {planeLabel(pool.plane)}
              {collectorId && <> · collector <span className="mono">{collectorId}</span></>}
            </p>
          )}

          {step === 'pool' && (
            <PoolStep page={pools.data!} chosen={poolId}
                      onChoose={(id) => set({ pool: id, collector: null })} />
          )}
          {step === 'firewall' && pool && (
            <FirewallStep pool={pool} onBuilt={() => setFirewallBuilt(true)} />
          )}
          {step === 'collector' && pool && (
            <CollectorStep pool={pool} chosen={collectorId}
                           onChoose={(id) => set({ collector: id })} />
          )}
          {step === 'enroll' && pool && collectorId && (
            <EnrollStep id={collectorId} q={collector} />
          )}
          {step === 'ranges' && pool && (
            <RangesStep pool={pool} collectorId={collectorId} readiness={readiness.data} />
          )}
          {step === 'credentials' && pool && (
            <CredentialsStep readiness={readiness.data} />
          )}
          {step === 'traps' && pool && collectorId && (
            <TrapsStep pool={pool} />
          )}
          {step === 'verify' && pool && (
            <VerifyStep readiness={readiness.data} collector={collector.data ?? null} />
          )}

          <div className="wizard-nav">
            <button disabled={index === 0} onClick={() => go(STEPS[index - 1].key)}>
              Back
            </button>
            <span className="spacer" />
            {index < STEPS.length - 1 && (
              <button className="primary"
                      disabled={needsPool(STEPS[index + 1].key)
                                || needsCollector(STEPS[index + 1].key)}
                      onClick={() => go(STEPS[index + 1].key)}>
                Next: {STEPS[index + 1].label}
              </button>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ steps */

const STEPS = [
  { key: 'pool', label: 'Pool' },
  { key: 'firewall', label: 'Firewall request' },
  { key: 'collector', label: 'Collector record' },
  { key: 'enroll', label: 'Install and preflight' },
  { key: 'ranges', label: 'Discovery ranges' },
  { key: 'credentials', label: 'Credentials' },
  { key: 'traps', label: 'Trap destinations' },
  { key: 'verify', label: 'Verify' },
] as const;

type StepKey = typeof STEPS[number]['key'];

/** true: done. false: checked and not true. null: nothing to judge yet. */
function markSteps({ pool, collector, readiness, firewallBuilt }: {
  pool: PoolRow | null;
  collector: CollectorDetail | null;
  readiness: PoolReadiness | null;
  firewallBuilt: boolean;
}): Record<StepKey, boolean | null> {
  const check = (key: string) => readiness?.checks.find((c) => c.key === key)?.ok ?? null;
  const inPool = !!collector && !!pool && collector.pool_id === pool.id;
  return {
    pool: pool ? true : null,
    // The matrix is a document handed to another team; the platform cannot
    // see whether the rules went in. Building it is all this can mark.
    firewall: firewallBuilt ? true : null,
    collector: collector ? inPool : null,
    enroll: !collector ? null
      : collector.cert_serial && collector.has_run && collector.preflight?.passed ? true
        : collector.preflight && !collector.preflight.passed ? false : null,
    ranges: !readiness ? null : readiness.ranges.length === 0 ? null : check('endpoints'),
    credentials: check('credentials'),
    // Where a device sends its traps is set on the device; nothing here can
    // see it until one arrives, which the verify step reports.
    traps: null,
    verify: readiness ? (readiness.ready ? true : null) : null,
  };
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

function copy(text: string, mark: (v: boolean) => void) {
  void navigator.clipboard?.writeText(text).then(() => {
    mark(true);
    setTimeout(() => mark(false), 1500);
  });
}

/* ------------------------------------------------------------------- pool */

function PoolStep({ page, chosen, onChoose }: {
  page: PoolsPage;
  chosen: string | null;
  onChoose: (id: string) => void;
}) {
  const qc = useQueryClient();
  const [site, setSite] = useState(page.sites[0]?.id ?? '');
  const [plane, setPlane] = useState<PoolPlane>(page.planes[0] ?? 'it_oob');
  const [name, setName] = useState('');
  const siteCode = page.sites.find((s) => s.id === site)?.code ?? '';
  const taken = page.pools.find((p) => p.datacenter_id === site && p.plane === plane);
  const create = useMutation({
    mutationFn: () => api.createPool({
      name: name.trim() || `${siteCode}/${planeLabel(plane)}`, datacenter_id: site, plane,
    }),
    onSuccess: (p) => { void qc.invalidateQueries({ queryKey: ['pools'] }); onChoose(p.id); },
  });

  return (
    <>
      <div>
        <h3>Which network are you bringing up?</h3>
        <p className="muted">
          A pool is one management network at one site - the IT out-of-band VLAN,
          or the BMS network. Its collectors serve that network and nothing else,
          and every later step is scoped to it.
        </p>
      </div>

      {page.pools.length > 0 && (
        <div className="choice-list" role="radiogroup" aria-label="Existing pools">
          {page.pools.map((p) => (
            <label key={p.id} className={p.id === chosen ? 'chosen' : ''}>
              <input type="radio" name="pool" checked={p.id === chosen}
                     onChange={() => onChoose(p.id)} />
              <span>
                <strong>{p.name}</strong>{' '}
                <span className="muted">{p.site} · {planeLabel(p.plane)}</span>
              </span>
              <span className="muted">
                {p.members.length} collector{p.members.length === 1 ? '' : 's'} ·{' '}
                {p.endpoints} endpoint{p.endpoints === 1 ? '' : 's'}
              </span>
            </label>
          ))}
        </div>
      )}

      <fieldset className="proto">
        <legend>New pool</legend>
        {create.error && <div className="banner">{errorText(create.error, 'Creating a pool')}</div>}
        <div className="form-grid">
          <label>
            <span>Site</span>
            <select value={site} onChange={(e) => setSite(e.target.value)}>
              {page.sites.map((s) => <option key={s.id} value={s.id}>{s.code} · {s.name}</option>)}
            </select>
          </label>
          <label>
            <span>Plane</span>
            <select value={plane} onChange={(e) => setPlane(e.target.value as PoolPlane)}>
              {page.planes.map((p) => <option key={p} value={p}>{planeLabel(p)}</option>)}
            </select>
            <em className={taken ? 'hint bad' : 'hint'}>
              {taken ? `${siteCode} already has one: ${taken.name}. Pick it above.`
                : 'The network it covers. Discovery ranges with this purpose at this site feed it.'}
            </em>
          </label>
          <label>
            <span>Name</span>
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={64}
                   placeholder={`${siteCode}/${planeLabel(plane)}`} />
          </label>
        </div>
        <div style={{ marginTop: 10 }}>
          <button className="primary" disabled={!site || !!taken || create.isPending}
                  onClick={() => create.mutate()}>
            {create.isPending ? 'Creating…' : 'Create pool'}
          </button>
        </div>
      </fieldset>
    </>
  );
}

/* --------------------------------------------------------------- firewall */

function FirewallStep({ pool, onBuilt }: { pool: PoolRow; onBuilt: () => void }) {
  const [copied, setCopied] = useState(false);
  const matrix = useQuery<FirewallMatrix>({
    queryKey: ['pool-firewall', pool.id],
    queryFn: async () => { const m = await api.poolFirewallMatrix(pool.id); onBuilt(); return m; },
  });

  const m = matrix.data;
  return (
    <>
      <div>
        <h3>Raise the change requests</h3>
        <p className="muted">
          In most sites this is the longest step - days to weeks - so it comes
          before anything is installed. The network team opens the collector's
          path out to this platform and in to the devices; for a BMS pool,
          facilities approves a read-only pinhole to the supervisor and
          gateways, since they own that network, not IT.
        </p>
      </div>
      {pool.endpoints === 0 && (
        <div className="banner soft">
          No device resolves into this pool yet, so the matrix only has the
          collector-to-core rules. Come back after the discovery-ranges step and
          rebuild it - the device-side rows are derived from those ranges.
        </div>
      )}
      {matrix.isLoading && <p className="muted">Building…</p>}
      {matrix.error && <div className="banner">{errorText(matrix.error, 'Building the matrix')}</div>}
      {m && (
        <>
          <div style={{ display: 'flex', gap: 8 }}>
            <button onClick={() => copy(m.text, setCopied)}>
              {copied ? 'Copied' : 'Copy as change request'}
            </button>
            <button onClick={() => downloadText(
              `firewall-${pool.site}-${pool.plane}.txt`, m.text)}>
              Download
            </button>
            <button onClick={() => void matrix.refetch()}>Rebuild</button>
          </div>
          <div className="table-frame">
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
        </>
      )}
    </>
  );
}

/* -------------------------------------------------------------- collector */

function CollectorStep({ pool, chosen, onChoose }: {
  pool: PoolRow;
  chosen: string | null;
  onChoose: (id: string) => void;
}) {
  const qc = useQueryClient();
  const fleet = useQuery<CollectorsPage>({ queryKey: ['collectors'], queryFn: () => api.collectors() });
  const suggested = `${pool.site}-${pool.plane.replace('_', '-')}-${String(pool.members.length + 1).padStart(2, '0')}`
    .toLowerCase();
  const [id, setId] = useState('');
  const [enrollment, setEnrollment] = useState<{ id: string; e: EnrollmentToken } | null>(null);
  const [copied, setCopied] = useState(false);

  const existing = new Set((fleet.data?.collectors ?? []).map((c) => c.id));
  const newId = id.trim() || suggested;
  const idError = !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(newId)
    ? "Letters, digits, '-' and '_' only."
    : existing.has(newId) ? 'A collector with this id already exists.' : null;
  // Only collectors with no pool: moving one out of another pool strands
  // that pool's devices, which is a decision for the Collectors page.
  const unplaced = (fleet.data?.collectors ?? []).filter(
    (c) => !c.pool_id && c.state !== 'decommissioned'
      && (!c.datacenter_id || c.datacenter_id === pool.datacenter_id));

  const done = (cid: string) => {
    void qc.invalidateQueries({ queryKey: ['pools'] });
    void qc.invalidateQueries({ queryKey: ['collectors'] });
    onChoose(cid);
  };
  const create = useMutation({
    mutationFn: () => api.createCollector(newId, null, pool.id),
    onSuccess: (r) => { setEnrollment({ id: r.id, e: r.enrollment }); done(r.id); },
  });
  const place = useMutation({
    mutationFn: (cid: string) => api.patchCollector(cid, { pool_id: pool.id }),
    onSuccess: (_r, cid) => done(cid),
  });
  const reissue = useMutation({
    mutationFn: (cid: string) => api.issueEnrollmentToken(cid),
    onSuccess: (e, cid) => setEnrollment({ id: cid, e }),
  });
  const members = pool.members;
  const chosenRow = fleet.data?.collectors.find((c) => c.id === chosen);

  return (
    <>
      <div>
        <h3>Create the collector's record</h3>
        <p className="muted">
          The record comes first and is the approval: the collector is placed in
          this pool before it ever runs, so its first assignment is the right one
          and nothing has to be moved later. Name it for where it runs - the site
          engineer will see this id on the VM.
        </p>
      </div>

      {enrollment && (
        <div className="banner soft">
          <p style={{ margin: '0 0 8px' }}>
            Install command for <span className="mono">{enrollment.id}</span> - shown
            once, expires {untilTime(enrollment.e.expires_at)}. Send it to the
            engineer who will run it on the collector's VM. It generates the key
            pair there; the private key never leaves that host.
          </p>
          <div className="copyable">
            <code>{enrollment.e.install_command}</code>
            <button onClick={() => copy(enrollment.e.install_command, setCopied)}>
              {copied ? 'Copied' : 'Copy'}
            </button>
          </div>
        </div>
      )}

      {members.length > 0 && (
        <div className="choice-list" role="radiogroup" aria-label="Collectors in this pool">
          {members.map((m) => (
            <label key={m.collector_id} className={m.collector_id === chosen ? 'chosen' : ''}>
              <input type="radio" name="collector" checked={m.collector_id === chosen}
                     onChange={() => onChoose(m.collector_id)} />
              <span className="mono">{m.collector_id}</span>
              <span className="muted">
                {m.state}{!m.has_run ? ' · never checked in'
                  : m.healthy ? ' · healthy' : ' · silent'}
              </span>
            </label>
          ))}
        </div>
      )}
      {chosenRow && !chosenRow.cert_serial && !enrollment && (
        <p className="muted">
          <span className="mono">{chosenRow.id}</span> has not enrolled yet
          {chosenRow.has_pending_token ? ', and an unused token is outstanding' : ''}.
          If that command was lost or expired:{' '}
          <button disabled={reissue.isPending} onClick={() => reissue.mutate(chosenRow.id)}>
            Issue a new install command
          </button>
        </p>
      )}
      {reissue.error && <div className="banner">{errorText(reissue.error, 'Issuing a token')}</div>}

      <fieldset className="proto">
        <legend>{members.length ? 'Add another collector' : 'New collector'}</legend>
        {create.error && <div className="banner">{errorText(create.error, 'Creating a collector')}</div>}
        <div className="form-grid">
          <label>
            <span>Collector id</span>
            <input value={id} onChange={(e) => setId(e.target.value)} placeholder={suggested}
                   className="mono" />
            <em className={idError ? 'hint bad' : 'hint'}>
              {idError ?? (members.length
                ? 'A second member is standby: failover, and the pool can require it (minimum members).'
                : 'One VM per pool, with a static address on this network.')}
            </em>
          </label>
        </div>
        <div style={{ marginTop: 10 }}>
          <button className="primary" disabled={!!idError || create.isPending}
                  onClick={() => create.mutate()}>
            {create.isPending ? 'Creating…' : 'Create and get install command'}
          </button>
        </div>
      </fieldset>

      {unplaced.length > 0 && (
        <fieldset className="proto">
          <legend>Or place a collector that already runs</legend>
          {place.error && <div className="banner">{errorText(place.error, 'Placing a collector')}</div>}
          <p className="muted" style={{ marginTop: 0 }}>
            These have no pool, so today they serve every plane at their site.
            Placing one here narrows it to this network only.
          </p>
          <table>
            <tbody>
              {unplaced.map((c) => (
                <tr key={c.id}>
                  <td className="mono">{c.id}</td>
                  <td><StateBadge row={c} /></td>
                  <td className="muted">{c.site ?? 'any site'}</td>
                  <td className="num">
                    <button disabled={place.isPending} onClick={() => place.mutate(c.id)}>
                      Place here
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </fieldset>
      )}
    </>
  );
}

/* ----------------------------------------------------------------- enroll */

function EnrollStep({ id, q }: { id: string; q: UseQueryResult<CollectorDetail> }) {
  const qc = useQueryClient();
  const approve = useMutation({
    mutationFn: () => api.patchCollector(id, { state: 'active' }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['collector', id] }),
  });
  if (q.isLoading) return <p className="muted">Loading…</p>;
  if (q.error || !q.data) return <div className="banner">Could not load {id}.</div>;
  const d = q.data;

  const items = [
    { name: 'Enrolled', ok: d.cert_serial ? true : null,
      detail: d.cert_serial
        ? `certificate issued ${relativeTime(d.enrolled_at)}, valid until ${untilTime(d.cert_not_after)}`
        : 'waiting for dcim-collector enroll on the VM' },
    { name: 'First heartbeat', ok: d.has_run ? (d.alive ? true : false) : null,
      detail: !d.has_run ? 'not yet - starts once the service is running'
        : d.alive ? `last ${relativeTime(d.last_heartbeat)}, running ${d.build ?? 'unknown build'}`
          : `silent since ${relativeTime(d.last_heartbeat)}` },
    { name: 'Version', ok: d.version_skew === 'unknown' ? null
        : d.version_skew === 'current' || d.version_skew === 'supported',
      detail: `${humanise(d.version_skew)} against platform ${d.platform_version}` },
    { name: 'Preflight', ok: d.preflight ? d.preflight.passed : null,
      detail: !d.preflight ? 'runs automatically after enroll'
        : d.preflight.passed ? `passed ${relativeTime(d.preflight.ran_at)}`
          : `findings ${relativeTime(d.preflight.ran_at)} - see below` },
  ];

  return (
    <>
      <div>
        <h3>Install, enroll, preflight</h3>
        <p className="muted">
          On the collector's VM, the site engineer runs the install command. This
          page checks every five seconds and fills in as it happens - no one has
          to report back.
        </p>
      </div>
      {d.state === 'pending' && (
        <div className="banner soft">
          <StateBadge row={d} /> It will not be given work until approved.{' '}
          <button disabled={approve.isPending} onClick={() => approve.mutate()}>Approve</button>
          {approve.error && <> {errorText(approve.error, 'Approving')}</>}
        </div>
      )}
      <ul className="checks">
        {items.map((c) => (
          <li key={c.name} className={c.ok === true ? 'ok' : c.ok === false ? 'bad' : 'pending'}>
            <span className="name">{c.name}</span>
            <span className="detail">{c.detail}</span>
          </li>
        ))}
      </ul>
      <Preflight d={d} />
      {d.preflight && !d.preflight.passed && (
        <p className="muted">
          <Tip tip={oneLine(`Reachability fails with every sample address silent
                  when a firewall rule or BMS pinhole is missing; with some
                  silent when the path is open and devices are down.`)}>
            A failed reachability check usually means a change request from the
            previous step is not in yet.
          </Tip>{' '}
          Fix it, then have the engineer rerun <span className="mono">dcim-collector preflight</span>;
          the result replaces this one.
        </p>
      )}
      <p><Link to={`/settings/collectors/${encodeURIComponent(id)}`}>Collector detail →</Link></p>
    </>
  );
}

/* ----------------------------------------------------------------- ranges */

function RangesStep({ pool, collectorId, readiness }: {
  pool: PoolRow;
  collectorId: string | null;
  readiness: PoolReadiness | undefined;
}) {
  const qc = useQueryClient();
  const [cidr, setCidr] = useState('');
  const [name, setName] = useState('');
  const add = useMutation({
    mutationFn: () => api.createDiscoveryRange({
      cidr: cidr.trim(), name: name.trim() || cidr.trim(),
      datacenter_id: pool.datacenter_id, purpose: pool.plane,
      collector_id: collectorId, exclusions: [], enabled: true,
    }),
    onSuccess: () => {
      setCidr(''); setName('');
      void qc.invalidateQueries({ queryKey: ['pool-readiness', pool.id] });
      void qc.invalidateQueries({ queryKey: ['pools'] });
    },
  });
  const ranges = readiness?.ranges ?? [];
  const total = (readiness?.protocols ?? []).reduce((n, p) => n + p.endpoints, 0);

  return (
    <>
      <div>
        <h3>Tell it where the devices are</h3>
        <p className="muted">
          A discovery range at {pool.site} with purpose {planeLabel(pool.plane)} is
          what makes a device's endpoints belong to this pool. Sweep it, review
          what answered, and promote - promoted devices land here and the pool's
          collectors take them over automatically.
        </p>
      </div>
      {ranges.length > 0 ? (
        <table>
          <thead><tr><th>Range</th><th>Name</th><th>Enabled</th></tr></thead>
          <tbody>
            {ranges.map((r) => (
              <tr key={r.id}>
                <td className="mono">{r.cidr}</td>
                <td>{r.name}</td>
                <td className={r.enabled ? '' : 'muted'}>{r.enabled ? 'yes' : 'no'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : <p className="muted">No range feeds this pool yet.</p>}

      <fieldset className="proto">
        <legend>Add range</legend>
        {add.error && <div className="banner">{errorText(add.error, 'Adding a range')}</div>}
        <div className="form-grid">
          <label>
            <span>CIDR</span>
            <input value={cidr} onChange={(e) => setCidr(e.target.value)} className="mono"
                   placeholder={pool.plane === 'bms' ? '10.52.3.0/24' : '10.51.3.0/24'} />
            <em className="hint">
              The management subnet itself, not the whole site. Swept by{' '}
              {collectorId ? <span className="mono">{collectorId}</span> : 'any collector'}.
            </em>
          </label>
          <label>
            <span>Name</span>
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={64}
                   placeholder={`${pool.site} ${planeLabel(pool.plane)}`} />
          </label>
        </div>
        <div style={{ marginTop: 10 }}>
          <button className="primary" disabled={!cidr.trim() || add.isPending}
                  onClick={() => add.mutate()}>
            {add.isPending ? 'Adding…' : 'Add range'}
          </button>
        </div>
      </fieldset>

      <p>
        {total > 0
          ? <>{total} endpoint{total === 1 ? '' : 's'} already resolve here. </>
          : <>Nothing resolves here yet. </>}
        <Link to="/assets/discovery">Sweep and promote in Discovery →</Link>
      </p>
      {pool.plane === 'bms' && (
        <p className="muted">
          Plant behind a Modbus gateway shares the gateway's address, so a sweep
          finds the gateway, not the chillers behind it. Those are added as
          devices with their unit IDs from the plant's register list.
        </p>
      )}
    </>
  );
}

/* ------------------------------------------------------------ credentials */

function CredentialsStep({ readiness }: { readiness: PoolReadiness | undefined }) {
  if (!readiness) return <p className="muted">Loading…</p>;
  const protos = readiness.protocols;
  const missing = protos.reduce((n, p) => n + p.missing_credential, 0);
  return (
    <>
      <div>
        <h3>Credentials</h3>
        <p className="muted">
          What the collector presents to each device. SNMP needs a community or,
          better, a v3 read-only user; Redfish a ReadOnly-role account on the BMC;
          gNMI a username or client certificate. BACnet/IP and Modbus/TCP have no
          authentication at all - access control for them is the firewall pinhole.
        </p>
      </div>
      {protos.length === 0 ? (
        <p className="muted">No endpoints resolve into this pool yet - add a range first.</p>
      ) : (
        <table>
          <thead>
            <tr><th>Protocol</th><th className="num">Endpoints</th>
                <th className="num">With credential</th><th className="num">Missing</th></tr>
          </thead>
          <tbody>
            {protos.map((p) => (
              <tr key={p.protocol}>
                <td>{p.protocol}</td>
                <td className="num">{p.endpoints}</td>
                <td className="num">{p.needs_credential ? p.with_credential : <span className="muted">not used</span>}</td>
                <td className="num">
                  {!p.needs_credential ? <span className="muted">—</span>
                    : p.missing_credential > 0 ? <span className="warn">{p.missing_credential}</span>
                      : <span className="ok">0</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {missing > 0 && (
        <div className="banner soft">
          {missing} endpoint{missing === 1 ? '' : 's'} will be polled with nothing to
          authenticate with. A wrong or absent SNMP community is not refused - the
          agent drops the request, so the device looks dead rather than misconfigured.
        </div>
      )}
      <p className="muted">
        Credentials are attached per endpoint today: at promote time in Discovery,
        or afterwards in the device's endpoint editor. Per-pool credential sets
        (docs/26 Phase 4) are not built yet.
      </p>
    </>
  );
}

/* ------------------------------------------------------------------ traps */

function TrapsStep({ pool }: { pool: PoolRow }) {
  const fleet = useQuery<CollectorsPage>({ queryKey: ['collectors'], queryFn: () => api.collectors() });
  const members = (fleet.data?.collectors ?? []).filter((c) => c.pool_id === pool.id);
  const snmp = (pool.protocols['snmp'] ?? 0) > 0;
  const redfish = (pool.protocols['redfish'] ?? 0) > 0;

  return (
    <>
      <div>
        <h3>Where devices send events</h3>
        <p className="muted">
          Polling finds a fault within one interval; a trap or event finds it in
          seconds. Devices push to an address that is configured on each device,
          so this step is instructions for whoever owns them.
        </p>
      </div>
      <fieldset className="proto">
        <legend>SNMP traps</legend>
        {pool.trap_vip ? (
          <p>
            Every device in this pool sends to the pool's trap VIP{' '}
            <span className="mono">{pool.trap_vip}</span>, UDP port below. The VIP
            follows whichever member is active, so a failover needs no device change.
          </p>
        ) : (
          <p>
            No trap VIP: each device sends to a collector's own address on this
            network. With more than one member, set a VIP on the Pools page instead,
            or a failover silently loses traps until someone reconfigures devices.
          </p>
        )}
        {!snmp && <p className="muted">No SNMP endpoints resolve here yet.</p>}
        <table>
          <thead><tr><th>Collector</th><th>Host</th><th>Trap listener</th><th>Redfish events</th></tr></thead>
          <tbody>
            {members.map((c) => {
              const t = (c.effective['snmp_trap'] ?? {}) as Record<string, unknown>;
              const r = (c.effective['redfish_event'] ?? {}) as Record<string, unknown>;
              return (
                <tr key={c.id}>
                  <td className="mono">{c.id}</td>
                  <td className="mono muted">{c.hostname ?? '—'}</td>
                  <td className="mono">
                    {!c.has_run ? <span className="muted">not reported yet</span>
                      : t['enabled'] === false ? <span className="warn">disabled</span>
                        : String(t['listen'] ?? '—')}
                  </td>
                  <td className="mono">
                    {!c.has_run ? <span className="muted">—</span>
                      : r['enabled'] ? String(r['advertise'] || r['listen'] || '—')
                        : <span className="muted">off</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <p className="muted">
          Use SNMPv3 (authPriv) for traps wherever the device supports it; a v2c
          trap is readable by anything on the path.
        </p>
      </fieldset>
      {redfish && (
        <fieldset className="proto">
          <legend>Redfish events</legend>
          <p className="muted" style={{ margin: 0 }}>
            Nothing to configure on the BMCs: the collector creates and reconciles
            each BMC's event subscription itself when Redfish events are enabled.
          </p>
        </fieldset>
      )}
      {pool.plane === 'bms' && (
        <fieldset className="proto">
          <legend>BACnet and Modbus</legend>
          <p className="muted" style={{ margin: 0 }}>
            Modbus has no push at all, and BACnet alarms reach this platform by
            polling here - both are covered by the poll interval, not by this step.
          </p>
        </fieldset>
      )}
    </>
  );
}

/* ----------------------------------------------------------------- verify */

const CHECK_NAME: Record<string, string> = {
  members: 'Collectors accepting work',
  preflight: 'Preflight',
  endpoints: 'Devices in the pool',
  credentials: 'Credentials',
  assigned: 'Every endpoint owned',
  online: 'Endpoints online',
};

function VerifyStep({ readiness, collector }: {
  readiness: PoolReadiness | undefined;
  collector: CollectorDetail | null;
}) {
  if (!readiness) return <p className="muted">Loading…</p>;
  const traps = collector?.stats.traps_received;
  return (
    <>
      <div>
        <h3>Verify</h3>
        <p className="muted">
          Rechecked every ten seconds against what the collectors actually report.
          Undecided means there is nothing to judge yet, never a pass.
        </p>
      </div>
      {readiness.ready && (
        <div className="banner" style={{ borderColor: 'var(--ok)' }}>
          This pool is collecting: every endpoint owned, authenticated and online.
        </div>
      )}
      <ul className="checks">
        {readiness.checks.map((c) => (
          <li key={c.key} className={c.ok === true ? 'ok' : c.ok === false ? 'bad' : 'pending'}>
            <span className="name">{CHECK_NAME[c.key] ?? humanise(c.key)}</span>
            <span className="detail">{c.detail}</span>
          </li>
        ))}
        {collector && (
          <li className={traps ? 'ok' : 'pending'}>
            <span className="name">Traps received</span>
            <span className="detail">
              {traps == null ? `${collector.id} has not reported a trap count`
                : traps > 0 ? `${traps} by ${collector.id} since it started`
                  : `none yet by ${collector.id} - send a test trap from one device`}
            </span>
          </li>
        )}
      </ul>
      {readiness.protocols.length > 0 && (
        <table>
          <thead>
            <tr><th>Protocol</th><th className="num">Online</th><th className="num">Degraded</th>
                <th className="num">Offline</th><th className="num">Never polled</th></tr>
          </thead>
          <tbody>
            {readiness.protocols.map((p) => (
              <tr key={p.protocol}>
                <td>{p.protocol}</td>
                <td className="num">{p.online}</td>
                <td className={p.degraded ? 'num warn' : 'num muted'}>{p.degraded}</td>
                <td className={p.offline ? 'num critical' : 'num muted'}>{p.offline}</td>
                <td className="num muted">{p.never_polled}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {readiness.members.length > 0 && (
        <p className="muted">
          Members:{' '}
          {readiness.members.map((m, i) => (
            <span key={m.collector_id}>
              {i > 0 && ', '}
              <Link to={`/settings/collectors/${encodeURIComponent(m.collector_id)}`}
                    className="mono">{m.collector_id}</Link>
              {' '}({!m.has_run ? 'never checked in' : m.healthy ? 'healthy' : 'silent'}
              {m.preflight_passed === false && ', preflight findings'})
            </span>
          ))}
        </p>
      )}
    </>
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
