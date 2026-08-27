import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ApiError,
  api,
  type AddressingField,
  type EndpointOptions,
  type EndpointPatch,
  type EndpointSummary,
} from '../../api/client';

/** Editing how a device is reached.
 *
 *  Deliberately one endpoint at a time. A server has an OS agent and a BMC, a
 *  gateway fronts eighteen field devices, and they fail independently - a form
 *  that edited "the device's SNMP settings" would be editing something that
 *  does not exist.
 *
 *  Nothing here is a listener. The trap port, the BACnet local port and the
 *  Redfish event advertise address belong to the collector process, not to any
 *  device: changing one is a contract with the whole device plane rather than
 *  with this row, and it belongs on a page that can say so. */
export function EndpointEditor({
  deviceId, endpoint, onClose,
}: {
  deviceId: string;
  endpoint: EndpointSummary;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  // Credential search, debounced. This estate holds 894 SNMP credentials -
  // one per device, because the community string is per-device - so the list
  // is filtered on the server and this is what filters it.
  const [credSearch, setCredSearch] = useState('');
  const search = useDebounced(credSearch, 250);

  const options = useQuery<EndpointOptions>({
    queryKey: ['endpoint-options', endpoint.protocol, search,
               endpoint.credential_id],
    queryFn: () => api.endpointOptions({
      protocol: endpoint.protocol, q: search || undefined,
      current: endpoint.credential_id ?? undefined,
    }),
    // Credentials and profiles change rarely; keep the previous list on screen
    // while a new search resolves so the field does not empty under the cursor.
    staleTime: 60_000,
    placeholderData: (prev) => prev,
  });

  const [form, setForm] = useState(() => ({
    address: endpoint.address ?? '',
    port: endpoint.port == null ? '' : String(endpoint.port),
    addressing: { ...endpoint.addressing } as Record<string, string>,
    credential_id: endpoint.credential_id ?? '',
    poll_profile_id: endpoint.poll_profile_id ?? '',
    admin_state: endpoint.admin_state,
  }));

  const behindGateway = Boolean(endpoint.via_endpoint_id);
  const isTrap = endpoint.protocol === 'snmp_trap';
  const defaultPort = options.data?.default_ports[endpoint.protocol];
  const fields: Record<string, AddressingField> =
    options.data?.addressing[endpoint.protocol] ?? {};

  // Already narrowed to this protocol by the server. The mismatch is refused
  // there too, but an operator should not be able to pick one and only then be
  // told what they picked cannot work.
  const credentials = options.data?.credentials ?? [];
  const credTotal = options.data?.credential_total ?? credentials.length;
  const capped = credTotal > credentials.length;
  const profiles = options.data?.poll_profiles ?? [];

  const errors = useMemo(() => validate(form, fields), [form, fields]);
  const patch = useMemo(
    () => diff(endpoint, form, behindGateway, isTrap),
    [endpoint, form, behindGateway, isTrap],
  );
  const changed = Object.keys(patch).length > 0;

  const save = useMutation({
    mutationFn: () => api.updateEndpoint(deviceId, endpoint.id, patch),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['device', deviceId] });
      qc.invalidateQueries({ queryKey: ['device-endpoints', deviceId] });
      onClose();
    },
  });

  const set = (k: keyof typeof form, v: string) =>
    setForm((f) => ({ ...f, [k]: v }));
  const setAddr = (k: string, v: string) =>
    setForm((f) => ({ ...f, addressing: { ...f.addressing, [k]: v } }));

  return (
    <div className="sheet-scrim" role="dialog" aria-modal="true"
         aria-label={`Edit ${endpoint.protocol} endpoint`}
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="sheet narrow">
        <header className="sheet-head">
          <div>
            <h2>{endpoint.protocol} · {endpoint.role.replace(/_/g, ' ')}</h2>
            <p>
              How the collector reaches this device on {endpoint.protocol}.
              Saved changes reach it on its next assignment fetch — within
              about thirty seconds, with nothing restarted.
            </p>
          </div>
          <button className="close" onClick={onClose} aria-label="Close">✕</button>
        </header>

        <div className="sheet-body">
          {options.isLoading && <p className="muted">Loading…</p>}
          {save.error && (
            <div className="banner">
              {save.error instanceof ApiError && save.error.status === 403
                ? 'Changing connection settings needs an operator account.'
                : String((save.error as Error).message)}
            </div>
          )}

          {behindGateway && (
            <div className="banner soft">
              Reached through <strong>{endpoint.via_name ?? 'a gateway'}</strong>.
              The address on the wire is the gateway's, so it is set there —
              here you choose which device behind it answers.
            </div>
          )}

          <div className="form-grid">
            <label>
              <span>Address</span>
              <input value={form.address} disabled={behindGateway}
                     onChange={(e) => set('address', e.target.value)}
                     placeholder={behindGateway ? 'via the gateway' : '10.50.0.1'} />
            </label>

            <label>
              <span>Port</span>
              <input value={form.port} inputMode="numeric"
                     disabled={behindGateway || isTrap}
                     onChange={(e) => set('port', e.target.value)}
                     placeholder={defaultPort ? `${defaultPort} (default)` : ''} />
              <em className="hint">
                {isTrap
                  ? 'A trap endpoint receives; the port that decides where traps '
                    + "arrive is the collector's listener, not this device's."
                  : 'Leave empty to follow the protocol default.'}
              </em>
            </label>

            {Object.entries(fields).map(([key, spec]) => (
              <label key={key}>
                <span>{spec.label}</span>
                <input value={form.addressing[key] ?? ''}
                       inputMode={spec.kind === 'text' ? 'text' : 'numeric'}
                       onChange={(e) => setAddr(key, e.target.value)} />
                <em className={errors[key] ? 'hint bad' : 'hint'}>
                  {errors[key] ?? spec.help}
                </em>
              </label>
            ))}

            <label>
              <span>Credential</span>
              {credTotal > 20 && (
                <input value={credSearch} placeholder="Search credentials…"
                       aria-label="Search credentials"
                       onChange={(e) => setCredSearch(e.target.value)} />
              )}
              <select value={form.credential_id}
                      onChange={(e) => set('credential_id', e.target.value)}>
                <option value="">None</option>
                {credentials.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name} · {c.kind}{c.secret_hint ? ` (${c.secret_hint})` : ''}
                  </option>
                ))}
              </select>
              <em className="hint">
                {credentials.length === 0
                  ? `No ${endpoint.protocol} credential matches that.`
                  : capped
                    ? `Showing ${credentials.length} of ${credTotal
                      .toLocaleString()} ${endpoint.protocol} credentials — `
                      + 'search to narrow. Only credentials for this protocol '
                      + 'are offered: one lent from another is a 401 on every '
                      + 'poll.'
                    : 'Only credentials for this protocol are offered — one lent '
                      + 'from another is a 401 on every poll, not a weaker login.'}
              </em>
            </label>

            <label>
              <span>Poll profile</span>
              <select value={form.poll_profile_id}
                      onChange={(e) => set('poll_profile_id', e.target.value)}>
                {profiles.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} · {p.push_enabled && p.interval_s === 0
                      ? 'device pushes'
                      : `every ${p.interval_s}s`}
                  </option>
                ))}
              </select>
              <em className="hint">
                Shared with {profileUse(profiles, form.poll_profile_id)} — editing
                the profile itself would move all of them.
              </em>
            </label>

            <label>
              <span>Administrative state</span>
              <select value={form.admin_state}
                      onChange={(e) => set('admin_state', e.target.value)}>
                <option value="enabled">Enabled — polled normally</option>
                <option value="maintenance">
                  Maintenance — polled, alarms suppressed
                </option>
                <option value="disabled">Disabled — not polled at all</option>
              </select>
              <em className="hint">
                {form.admin_state === 'disabled'
                  ? 'Nothing will be collected on this protocol. Metrics behind '
                    + 'it stop updating and stale-data alarms follow.'
                  : form.admin_state === 'maintenance'
                    ? 'For planned work: the poll continues so history has no '
                      + 'hole, but the endpoint raises nothing.'
                    : 'Polled on its profile; failures raise alarms.'}
              </em>
            </label>
          </div>
        </div>

        <div className="sheet-foot">
          <span className="muted">
            {changed
              ? `${Object.keys(patch).length} change${
                  Object.keys(patch).length === 1 ? '' : 's'} to save`
              : 'Nothing changed'}
          </span>
          <span className="spacer" />
          <button onClick={onClose}>Cancel</button>
          <button className="primary"
                  disabled={!changed || Object.keys(errors).length > 0
                            || save.isPending}
                  onClick={() => save.mutate()}>
            {save.isPending ? 'Saving…' : 'Save'}
          </button>
        </div>
      </section>
    </div>
  );
}

type Form = {
  address: string; port: string; addressing: Record<string, string>;
  credential_id: string; poll_profile_id: string; admin_state: string;
};

/** Client-side check against the same ranges the server rejects with, so a
 *  typo is caught while the operator is still looking at the field. The server
 *  still validates: this is a courtesy, not the boundary. */
function validate(form: Form, fields: Record<string, AddressingField>) {
  const errors: Record<string, string> = {};
  for (const [key, spec] of Object.entries(fields)) {
    const raw = form.addressing[key];
    if (raw == null || raw === '' || spec.kind === 'text') continue;
    const n = Number(raw);
    if (!Number.isInteger(n)) errors[key] = `${spec.label} must be a whole number`;
    else if (spec.min != null && spec.max != null && (n < spec.min || n > spec.max))
      errors[key] = `${spec.label} must be between ${spec.min} and ${spec.max}`;
  }
  if (form.port !== '') {
    const p = Number(form.port);
    if (!Number.isInteger(p) || p < 1 || p > 65535)
      errors.port = 'Port must be between 1 and 65535';
  }
  return errors;
}

/** Only what the operator actually changed.
 *
 *  Sending the whole form would bump `updated_at` on every save, and that
 *  timestamp is what the assignment version derives from - a no-op save would
 *  hand every collector a fresh assignment, credentials included, for a form
 *  somebody opened and closed. */
function diff(current: EndpointSummary, form: Form,
              behindGateway: boolean, isTrap: boolean): EndpointPatch {
  const patch: EndpointPatch = {};

  if (!behindGateway) {
    const address = form.address.trim() || null;
    if (address !== (current.address ?? null)) patch.address = address;

    if (!isTrap) {
      const port = form.port.trim() === '' ? null : Number(form.port);
      if (port !== (current.port ?? null)) patch.port = port;
    }
  }

  const addressing: Record<string, string | number> = {};
  for (const [k, v] of Object.entries(form.addressing)) {
    if (v === '' || v == null) continue;
    addressing[k] = /^-?\d+$/.test(String(v)) ? Number(v) : v;
  }
  if (JSON.stringify(addressing) !== JSON.stringify(current.addressing ?? {}))
    patch.addressing = addressing;

  const cred = form.credential_id || null;
  if (cred !== (current.credential_id ?? null)) patch.credential_id = cred;

  if (form.poll_profile_id && form.poll_profile_id !== current.poll_profile_id)
    patch.poll_profile_id = form.poll_profile_id;

  if (form.admin_state !== current.admin_state) {
    patch.admin_state = form.admin_state;
    // `enabled` is the collector's own switch and admin_state is the operator
    // intent behind it. Kept in step here so a disabled endpoint is actually
    // dropped from the assignment rather than merely labelled.
    patch.enabled = form.admin_state !== 'disabled';
  }
  return patch;
}

function profileUse(
  profiles: { id: string; endpoints: number }[], id: string,
): string {
  const p = profiles.find((x) => x.id === id);
  if (!p) return 'other endpoints';
  return p.endpoints === 1 ? 'this endpoint only'
    : `${p.endpoints.toLocaleString()} endpoints`;
}


/** Hold a value still until typing stops.
 *
 *  Without it every keystroke in the credential search is a query against a
 *  table with nine hundred rows in it, and the answers arrive out of order. */
function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return settled;
}
