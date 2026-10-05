import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, api, type Credential, type CredentialSecret } from '../../api/client';
import { usePaged } from '../../components/Pagination';
import { oneLine, relativeTime } from '../../lib/format';
import { Tip } from '../../components/HoverTip';

/** Device credentials (docs/26 Phase 4): what devices are polled with.
 *
 *  Secrets go in and never come back out - every list shows the hint. An
 *  estate on SNMPv3 runs on a handful of these, one per site and device class,
 *  set as a pool's default (Settings > Pools); the per-device v2c communities
 *  the simulator importer creates are the legacy the list sorts last. */

const AUTH = ['sha256', 'sha384', 'sha512', 'sha224', 'sha', 'md5'];
const PRIV = ['aes128', 'aes256', 'aes192', 'aes256c', 'aes192c', 'des', 'none'];

const KIND_LABEL: Record<string, string> = {
  snmp_v3: 'SNMPv3', snmp_v2c: 'SNMP v2c', http_basic: 'HTTP basic',
};

interface Draft {
  community: string;
  username: string;
  password: string;
  security_name: string;
  auth_protocol: string;
  auth_key: string;
  priv_protocol: string;
  priv_key: string;
}

const EMPTY: Draft = {
  community: '', username: '', password: '', security_name: '',
  auth_protocol: 'sha256', auth_key: '', priv_protocol: 'aes128', priv_key: '',
};

function secretOf(kind: string, d: Draft): CredentialSecret {
  if (kind === 'snmp_v2c') return { community: d.community };
  if (kind === 'http_basic') return { username: d.username, password: d.password };
  return {
    security_name: d.security_name, auth_protocol: d.auth_protocol, auth_key: d.auth_key,
    ...(d.priv_protocol !== 'none' ? { priv_protocol: d.priv_protocol, priv_key: d.priv_key } : {}),
  };
}

/** The same rules the server applies, so a typo shows while it is typed. */
function problem(kind: string, d: Draft): string | null {
  if (kind === 'snmp_v2c') return d.community ? null : 'A community is required.';
  if (kind === 'http_basic') return d.username && d.password ? null : 'Username and password are required.';
  if (!d.security_name) return 'A security name (the USM user) is required.';
  if (d.auth_key.length < 8) return 'The auth passphrase needs at least 8 characters (RFC 3414).';
  if (d.priv_protocol !== 'none' && d.priv_key.length < 8) {
    return 'The privacy passphrase needs at least 8 characters.';
  }
  return null;
}

function SecretFields({ kind, d, set }: {
  kind: string; d: Draft; set: (k: keyof Draft, v: string) => void;
}) {
  if (kind === 'snmp_v2c') {
    return (
      <label><span>Community</span>
        <input type="password" autoComplete="off" value={d.community}
               onChange={(e) => set('community', e.target.value)} />
      </label>
    );
  }
  if (kind === 'http_basic') {
    return (
      <>
        <label><span>Username</span>
          <input autoComplete="off" value={d.username} onChange={(e) => set('username', e.target.value)} />
        </label>
        <label><span>Password</span>
          <input type="password" autoComplete="new-password" value={d.password}
                 onChange={(e) => set('password', e.target.value)} />
        </label>
      </>
    );
  }
  return (
    <>
      <label><span>Security name</span>
        <input autoComplete="off" value={d.security_name} placeholder="dcim-poll"
               onChange={(e) => set('security_name', e.target.value)} />
        <em className="hint">The USM user configured on the devices.</em>
      </label>
      <label><span>Auth protocol</span>
        <select value={d.auth_protocol} onChange={(e) => set('auth_protocol', e.target.value)}>
          {AUTH.map((p) => <option key={p} value={p}>{p.toUpperCase()}{p === 'md5' ? ' (weak)' : ''}</option>)}
        </select>
      </label>
      <label><span>Auth passphrase</span>
        <input type="password" autoComplete="new-password" value={d.auth_key}
               onChange={(e) => set('auth_key', e.target.value)} />
      </label>
      <label><span>Privacy protocol</span>
        <select value={d.priv_protocol} onChange={(e) => set('priv_protocol', e.target.value)}>
          {PRIV.map((p) => (
            <option key={p} value={p}>
              {p === 'none' ? 'None (authNoPriv)' : p.toUpperCase()}{p === 'des' ? ' (weak)' : ''}
            </option>
          ))}
        </select>
      </label>
      {d.priv_protocol !== 'none' && (
        <label><span>Privacy passphrase</span>
          <input type="password" autoComplete="new-password" value={d.priv_key}
                 onChange={(e) => set('priv_key', e.target.value)} />
        </label>
      )}
    </>
  );
}

function errorText(e: unknown): string {
  return e instanceof ApiError ? e.message : String(e);
}

export function Credentials() {
  const qc = useQueryClient();
  const [q, setQ] = useState('');
  const [kindFilter, setKindFilter] = useState('');
  const list = useQuery({
    queryKey: ['credentials', q, kindFilter],
    queryFn: () => api.credentials({ q: q || undefined, kind: kindFilter || undefined, limit: 500 }),
    placeholderData: (prev) => prev,
  });
  const rows = list.data?.credentials ?? [];
  const paged = usePaged(rows, { noun: 'credentials', always: true });

  const [name, setName] = useState('');
  const [kind, setKind] = useState('snmp_v3');
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const setD = (k: keyof Draft, v: string) => setDraft((d) => ({ ...d, [k]: v }));
  const create = useMutation({
    mutationFn: () => api.createCredential(name.trim(), kind, secretOf(kind, draft)),
    onSuccess: () => {
      setName(''); setDraft(EMPTY);
      void qc.invalidateQueries({ queryKey: ['credentials'] });
    },
  });
  const createProblem = !name.trim() ? 'A name is required.' : problem(kind, draft);

  const [rotating, setRotating] = useState<Credential | null>(null);
  const [rotDraft, setRotDraft] = useState<Draft>(EMPTY);
  const rotate = useMutation({
    mutationFn: () => api.rotateCredential(rotating!.id, secretOf(rotating!.kind, rotDraft)),
    onSuccess: () => {
      setRotating(null); setRotDraft(EMPTY);
      void qc.invalidateQueries({ queryKey: ['credentials'] });
    },
  });

  return (
    <div className="stack">
      <div>
        <h2>Credentials</h2>
        <p className="subtitle">
          <Tip tip={oneLine(`Secrets are sealed to each collector's own key on the way out and
                  are never shown again here - only a hint. A pool's default credential
                  (Settings > Pools) covers every device in it that has none of its own.`)}>
            What devices are polled with. Secrets go in; only hints come out.
          </Tip>
        </p>
      </div>

      <fieldset className="proto">
        <legend>Add a credential</legend>
        {create.error && <div className="banner">{errorText(create.error)}</div>}
        <div className="form-grid">
          <label><span>Name</span>
            <input value={name} onChange={(e) => setName(e.target.value)}
                   placeholder="DC1 BMS - SNMPv3 poll" />
          </label>
          <label><span>Kind</span>
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="snmp_v3">SNMPv3 (USM)</option>
              <option value="snmp_v2c">SNMP v2c community</option>
              <option value="http_basic">HTTP basic (Redfish)</option>
            </select>
            {kind === 'snmp_v2c' && (
              <em className="hint">v2c sends the community in clear text; prefer v3 where the devices support it.</em>
            )}
          </label>
          <SecretFields kind={kind} d={draft} set={setD} />
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
          <button className="primary" disabled={!!createProblem || create.isPending}
                  onClick={() => create.mutate()}>
            {create.isPending ? 'Saving…' : 'Add credential'}
          </button>
          {createProblem && (name || kind !== 'snmp_v3') && <span className="muted">{createProblem}</span>}
        </div>
      </fieldset>

      {rotating && (
        <fieldset className="proto">
          <legend>Rotate {rotating.name}</legend>
          <p className="muted">
            Every endpoint and pool using it gets the new secret on its collector's next
            assignment fetch. Change it on the devices first, or polls fail until you do.
          </p>
          {rotate.error && <div className="banner">{errorText(rotate.error)}</div>}
          <div className="form-grid">
            <SecretFields kind={rotating.kind} d={rotDraft}
                          set={(k, v) => setRotDraft((d) => ({ ...d, [k]: v }))} />
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
            <button className="primary" disabled={!!problem(rotating.kind, rotDraft) || rotate.isPending}
                    onClick={() => rotate.mutate()}>
              {rotate.isPending ? 'Rotating…' : 'Rotate'}
            </button>
            <button onClick={() => { setRotating(null); setRotDraft(EMPTY); }}>Cancel</button>
          </div>
        </fieldset>
      )}

      <fieldset className="proto">
        <legend>All credentials</legend>
        <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
          <input placeholder="Search names" value={q} onChange={(e) => setQ(e.target.value)} />
          <select value={kindFilter} onChange={(e) => setKindFilter(e.target.value)}>
            <option value="">Every kind</option>
            <option value="snmp_v3">SNMPv3</option>
            <option value="snmp_v2c">SNMP v2c</option>
            <option value="http_basic">HTTP basic</option>
          </select>
        </div>
        {list.error && <div className="banner">{errorText(list.error)}</div>}
        <div className="table-frame">
          <table>
            <thead>
              <tr>
                <th>Name</th><th>Kind</th><th>Hint</th>
                <th className="num">
                  <Tip tip="Endpoints pinned to it by their own credential">Endpoints</Tip>
                </th>
                <th>Pool default for</th><th>Rotated</th><th />
              </tr>
            </thead>
            <tbody>
              {paged.rows.map((c) => (
                <tr key={c.id}>
                  <td>{c.name}</td>
                  <td>{KIND_LABEL[c.kind] ?? c.kind}</td>
                  <td className={c.secret_hint?.includes('weak') ? 'mono warn' : 'mono muted'}>
                    {c.secret_hint ?? '—'}
                  </td>
                  <td className="num">{c.endpoints ?? 0}</td>
                  <td className="muted">{(c.default_for ?? []).join(', ') || '—'}</td>
                  <td className="muted">{c.rotated_at ? relativeTime(c.rotated_at) : 'never'}</td>
                  <td>
                    <button onClick={() => { setRotating(c); setRotDraft(EMPTY); }}>Rotate</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {paged.foot}
      </fieldset>
    </div>
  );
}
