import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  api,
  type EndpointChoice,
  type EndpointOptions,
  type PlannedEndpoint,
} from '../../../api/client';

/** One planned endpoint, as the operator has answered it. */
export interface MonitoringRow {
  plan: PlannedEndpoint;
  include: boolean;
  mode: 'address' | 'pool' | 'existing' | 'new';
  credentialId: string;
  community: string;
  username: string;
  password: string;
}

export function initialRows(plan: PlannedEndpoint[]): MonitoringRow[] {
  return plan.map((p) => ({
    plan: p,
    include: true,
    // What the sweep proved, where it proved anything; otherwise a choice the
    // operator has to make before Promote is offered.
    mode: p.suggested_credential?.mode ?? 'existing',
    credentialId: '', community: '', username: '', password: '',
  }));
}

/** Why a row cannot be sent yet, or null. */
export function rowProblem(r: MonitoringRow): string | null {
  if (!r.include) return null;
  if (r.mode === 'existing' && !r.credentialId) return 'choose a credential';
  if (r.mode === 'new' && r.plan.protocol === 'snmp' && !r.community.trim()) {
    return 'enter the community';
  }
  if (r.mode === 'new' && r.plan.protocol === 'redfish'
      && (!r.username.trim() || !r.password)) {
    return 'enter the username and password';
  }
  return null;
}

export function toRequest(r: MonitoringRow): EndpointChoice {
  const credential: EndpointChoice['credential'] =
    r.mode === 'address' ? { mode: 'address' }
      : r.mode === 'pool' && r.plan.suggested_credential?.mode === 'pool'
        ? { mode: 'pool', pool_id: r.plan.suggested_credential.pool_id }
      : r.mode === 'existing' ? { mode: 'existing', id: r.credentialId }
        : r.plan.protocol === 'snmp'
          ? { mode: 'new', community: r.community }
          : { mode: 'new', username: r.username.trim(), password: r.password };
  return { candidate_id: r.plan.candidate_id, credential };
}

/** How the record will be polled, decided with it.
 *
 *  Promotion used to create the device and nothing else, so it landed with no
 *  endpoint: nothing polled it, the commissioning soak had nothing to measure,
 *  and somebody had to find it again and re-enter a credential the sweep had
 *  just proved. The rows are the API's plan - which agent each probe is and the
 *  profile that polls it, by the importer's rules - so the dialog cannot
 *  disagree with the importer about how a BMC is polled.
 *
 *  The sweep names the credential that worked by reference only. Where that is
 *  enough to rebuild it (the community IS the address) it is pre-selected, and
 *  where a pool's SNMPv3 credential answered, inheriting it is - so the endpoint
 *  follows that credential's rotations instead of holding a copy; a
 *  Redfish login is never guessed, because a wrong one is a failed-login alarm
 *  on the BMC and, on some, a lockout.
 */
export function PromoteMonitoring({ loading, rows, onChange }: {
  loading: boolean;
  rows: MonitoringRow[];
  onChange: (rows: MonitoringRow[]) => void;
}) {
  const set = (i: number, patch: Partial<MonitoringRow>) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  if (loading) return <div className="asset-skeleton" style={{ height: 80 }} />;
  if (rows.length === 0) {
    return (
      <p className="muted">
        Nothing this sweep reached can be polled from here. Add endpoints on the
        asset once it is recorded.
      </p>
    );
  }
  return (
    <div className="disc-mon">
      {rows.map((r, i) => (
        <div key={r.plan.candidate_id}
             className={`disc-mon-row${r.include ? '' : ' is-off'}`}>
          <label className="disc-mon-head">
            <input type="checkbox" checked={r.include}
                   onChange={(e) => set(i, { include: e.target.checked })} />
            <span className="proto">{r.plan.protocol.toUpperCase()}</span>
            <code>{r.plan.address}:{r.plan.port}</code>
            {r.plan.scheme && <span className="muted">{r.plan.scheme}</span>}
            <span className="muted">· {r.plan.role.replace('_', ' ')} · {r.plan.poll_profile}</span>
          </label>
          {r.include && (
            <CredentialChoice row={r} onChange={(patch) => set(i, patch)} />
          )}
          <p className="disc-mon-note">{r.plan.credential_note}</p>
          {r.include && rowProblem(r) && (
            <p className="disc-bad">{rowProblem(r)}</p>
          )}
        </div>
      ))}
    </div>
  );
}

function CredentialChoice({ row, onChange }: {
  row: MonitoringRow; onChange: (patch: Partial<MonitoringRow>) => void;
}) {
  const protocol = row.plan.protocol;
  const [q, setQ] = useState('');
  const search = useDebounced(q, 250);
  // Server-filtered: an estate like this one holds one SNMP credential per
  // device, and a select of nine hundred is not a choice.
  const options = useQuery<EndpointOptions>({
    queryKey: ['endpoint-options', protocol, search],
    queryFn: () => api.endpointOptions({ protocol, q: search || undefined }),
    enabled: row.mode === 'existing',
    staleTime: 60_000,
    placeholderData: (prev) => prev,
  });
  const creds = options.data?.credentials ?? [];
  const total = options.data?.credential_total ?? creds.length;

  return (
    <div className="disc-mon-cred">
      <select value={row.mode} aria-label={`${protocol} credential`}
              onChange={(e) => onChange({ mode: e.target.value as MonitoringRow['mode'] })}>
        {row.plan.suggested_credential?.mode === 'address' && (
          <option value="address">Community = this address (what answered)</option>
        )}
        {row.plan.suggested_credential?.mode === 'pool' && (
          <option value="pool">The pool's SNMPv3 credential (what answered)</option>
        )}
        <option value="existing">An existing credential</option>
        <option value="new">{protocol === 'snmp' ? 'A new community' : 'A new login'}</option>
      </select>

      {row.mode === 'existing' && (
        <>
          <input type="search" value={q} onChange={(e) => setQ(e.target.value)}
                 placeholder="Search credential names" aria-label="Search credentials" />
          <select value={row.credentialId} aria-label="Credential"
                  onChange={(e) => onChange({ credentialId: e.target.value })}>
            <option value="">— choose —</option>
            {creds.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}{c.secret_hint ? ` · ${c.secret_hint}` : ''}
              </option>
            ))}
          </select>
          {total > creds.length && (
            <span className="muted">
              Showing {creds.length} of {total.toLocaleString()}; search to narrow.
            </span>
          )}
        </>
      )}

      {/* Typed once, encrypted on arrival and never shown again - the store keeps
          a hint, not the value. autoComplete off: a browser offering to save a
          BMC password into a personal profile is how it leaves the building. */}
      {row.mode === 'new' && protocol === 'snmp' && (
        <input type="password" value={row.community} autoComplete="off"
               onChange={(e) => onChange({ community: e.target.value })}
               placeholder="community" aria-label="SNMP community" />
      )}
      {row.mode === 'new' && protocol === 'redfish' && (
        <>
          <input value={row.username} autoComplete="off"
                 onChange={(e) => onChange({ username: e.target.value })}
                 placeholder="username" aria-label="Redfish username" />
          <input type="password" value={row.password} autoComplete="new-password"
                 onChange={(e) => onChange({ password: e.target.value })}
                 placeholder="password" aria-label="Redfish password" />
        </>
      )}
    </div>
  );
}

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}
