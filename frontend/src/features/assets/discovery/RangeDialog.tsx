import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  api,
  type DiscoveryRange,
  type RangeInput,
  type RangeOptions,
} from '../../../api/client';
import { Dialog, DialogActions } from '../components/Dialog';
import {
  parseCidr, parseExclusions, probeCount, PURPOSE_LABEL,
} from './ranges';

/** Add or edit a discovery range.
 *
 *  Every field is one a real discovery job carries (SolarWinds, LibreNMS,
 *  Device42): the address space, what it is for, which site it belongs to, the
 *  collector that can reach it, and what must not be probed. The collector is the
 *  one that matters most - a range swept from the wrong site hears nothing, and
 *  every device in it reads as missing.
 */
export function RangeDialog({ range, initialCidr, options, onClose }: {
  /** Editing this one; absent to add a new one. */
  range?: DiscoveryRange;
  /** Pre-fill for a new range, e.g. from a suggestion. */
  initialCidr?: string;
  options?: RangeOptions;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [cidr, setCidr] = useState(range?.cidr ?? initialCidr ?? '');
  const [name, setName] = useState(range?.name ?? '');
  const [purpose, setPurpose] = useState(range?.purpose ?? '');
  const [site, setSite] = useState(range?.datacenter_id ?? '');
  const [collector, setCollector] = useState(range?.collector_id ?? '');
  const [exclusions, setExclusions] = useState((range?.exclusions ?? []).join('\n'));
  const [enabled, setEnabled] = useState(range?.enabled ?? true);
  const [notes, setNotes] = useState(range?.notes ?? '');
  const [error, setError] = useState<string | null>(null);
  // Two clicks, not a browser confirm(): a modal blocks the page.
  const [confirmDelete, setConfirmDelete] = useState(false);

  const parsed = cidr.trim() ? parseCidr(cidr) : null;
  const ex = parsed && !parsed.error ? parseExclusions(exclusions, parsed.cidr)
    : { list: [], errors: [] };
  const probes = parsed && !parsed.error
    ? probeCount({ cidr: parsed.cidr, exclusions: ex.list }) : 0;
  const invalid = !parsed || Boolean(parsed.error) || ex.errors.length > 0;

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['discovery-ranges'] });
    qc.invalidateQueries({ queryKey: ['discovery-subnets'] });
    qc.invalidateQueries({ queryKey: ['discovery-schedules'] });
  };

  const body = (): RangeInput => ({
    cidr: parsed?.cidr, name: name.trim() || null,
    purpose: purpose || null, datacenter_id: site || null,
    collector_id: collector || null, exclusions: ex.list, enabled,
    notes: notes.trim() || null,
  });

  const save = useMutation({
    mutationFn: () => (range ? api.updateDiscoveryRange(range.id, body())
      : api.createDiscoveryRange(body())),
    onSuccess: () => { refresh(); onClose(); },
    onError: (e) => setError(String(e)),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteDiscoveryRange(range!.id),
    onSuccess: () => { refresh(); onClose(); },
    // A schedule that sweeps it refuses the delete, with the schedule's name.
    onError: (e) => { setConfirmDelete(false); setError(String(e)); },
  });

  const collectors = options?.collectors ?? [];
  // A range may name a collector that has not checked in yet - a new site being
  // stood up. Kept selectable rather than silently dropped from the form.
  const unknownCollector = collector && !collectors.some((c) => c.id === collector);

  return (
    <Dialog title={range ? `Edit ${range.name}` : 'Add a discovery range'} onClose={onClose}>
      <div className="asset-form">
        <label>
          <span>Range (CIDR)</span>
          <input value={cidr} autoFocus={!range} placeholder="10.51.8.0/22"
                 onChange={(e) => setCidr(e.target.value)}
                 aria-invalid={Boolean(parsed?.error)} />
        </label>
        <label>
          <span>Name</span>
          <input value={name} placeholder={parsed?.cidr || 'Hall A BMC network'}
                 onChange={(e) => setName(e.target.value)} />
        </label>
        {parsed?.error && <p className="disc-bad asset-form-wide">{parsed.error}</p>}

        <label>
          <span>Purpose</span>
          <select value={purpose} onChange={(e) => setPurpose(e.target.value)}>
            <option value="">— not set —</option>
            {(options?.purposes ?? Object.keys(PURPOSE_LABEL)).map((p) => (
              <option key={p} value={p}>{PURPOSE_LABEL[p] ?? p}</option>
            ))}
          </select>
        </label>
        <label>
          <span>Site</span>
          <select value={site} onChange={(e) => setSite(e.target.value)}>
            <option value="">— not set —</option>
            {(options?.datacenters ?? []).map((d) => (
              <option key={d.id} value={d.id}>{d.name}</option>
            ))}
          </select>
        </label>

        <label className="asset-form-wide">
          <span>Swept by</span>
          <select value={collector} onChange={(e) => setCollector(e.target.value)}>
            <option value="">Any collector</option>
            {collectors.map((c) => (
              <option key={c.id} value={c.id}>
                {c.id}{c.hostname ? ` (${c.hostname})` : ''}
                {c.healthy ? '' : ' - not checking in'}
              </option>
            ))}
            {unknownCollector && (
              <option value={collector}>{collector} - never checked in</option>
            )}
          </select>
        </label>
        <p className="muted asset-form-wide disc-form-note">
          Choose the collector on this network when there is more than one. A sweep
          from a collector that cannot reach the range hears nothing, and every
          device in it would read as missing.
        </p>

        <label className="asset-form-wide">
          <span>Do not probe</span>
          <textarea rows={3} value={exclusions}
                    onChange={(e) => setExclusions(e.target.value)}
                    placeholder={'10.51.8.1\n10.51.9.0/28'}
                    aria-invalid={ex.errors.length > 0} />
        </label>
        {ex.errors.map((m) => <p key={m} className="disc-bad asset-form-wide">{m}</p>)}
        <p className="muted asset-form-wide disc-form-note">
          Gateways and HSRP/VRRP addresses, and controllers known to misbehave when
          scanned. One address or CIDR per line. Only collectors that honour
          exclusions are given this range.
        </p>

        <label className="asset-form-wide">
          <span>Notes</span>
          <textarea rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
        </label>
        <label className="asset-check asset-form-wide">
          <input type="checkbox" checked={enabled}
                 onChange={(e) => setEnabled(e.target.checked)} />
          <span>Enabled - offered for sweeps, and swept by its schedules</span>
        </label>

        {parsed && !parsed.error && (
          <p className="muted asset-form-wide">
            {probes.toLocaleString()} address{probes === 1 ? '' : 'es'} probed per sweep
            {ex.list.length > 0 && <>, {ex.list.length} exclusion{ex.list.length === 1 ? '' : 's'} skipped</>}.
          </p>
        )}
        {range?.overlaps && range.overlaps.length > 0 && (
          <p className="muted asset-form-wide">
            Shares addresses with {range.overlaps.join(', ')}: a device in both is
            swept twice.
          </p>
        )}
        {error && <div className="banner asset-form-wide">{error}</div>}
      </div>
      <DialogActions>
        {range && (
          confirmDelete ? (
            <button type="button" className="danger" disabled={remove.isPending}
                    onClick={() => { setError(null); remove.mutate(); }}>
              {remove.isPending ? 'Deleting…' : 'Confirm delete'}
            </button>
          ) : (
            <button type="button" onClick={() => setConfirmDelete(true)}>Delete</button>
          )
        )}
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" className="primary" disabled={invalid || save.isPending}
                onClick={() => { setError(null); save.mutate(); }}>
          {save.isPending ? 'Saving…' : range ? 'Save' : 'Add range'}
        </button>
      </DialogActions>
    </Dialog>
  );
}
