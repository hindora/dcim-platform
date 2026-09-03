import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, api, type Supplier } from '../../../api/client';
import { Dialog, DialogActions } from '../components/Dialog';
import { DevicePicker } from '../components/DevicePicker';

/** Record a contract, and optionally what it covers.
 *
 *  Covering devices at creation matters: a contract that covers nothing
 *  protects nothing, and the moment it is saved empty somebody has to remember
 *  to come back. The picker is the same one the window form uses, so
 *  "everything of this model" is one selection rather than two hundred clicks.
 */
export function ContractForm({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [showPicker, setShowPicker] = useState(false);

  const [supplierId, setSupplierId] = useState('');
  const [reference, setReference] = useState('');
  const [kind, setKind] = useState('warranty');
  const [serviceLevel, setServiceLevel] = useState('');
  const [startDate, setStartDate] = useState(today());
  const [endDate, setEndDate] = useState(inYears(3));
  const [cost, setCost] = useState('');
  const [currency, setCurrency] = useState('');
  const [autoRenew, setAutoRenew] = useState(false);
  const [notes, setNotes] = useState('');
  const [deviceIds, setDeviceIds] = useState<string[]>([]);

  const { data: suppliers } = useQuery<{ items: Supplier[] }>({
    queryKey: ['suppliers'],
    queryFn: api.suppliers,
  });

  const create = useMutation({
    mutationFn: () => api.createContract({
      supplier_id: supplierId || null,
      reference,
      kind,
      service_level: serviceLevel || null,
      start_date: startDate,
      end_date: endDate,
      cost: cost ? Number(cost) : null,
      currency: currency || null,
      auto_renew: autoRenew,
      notes: notes || null,
      device_ids: deviceIds,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['contracts'] });
      qc.invalidateQueries({ queryKey: ['asset-summary'] });
      // Cover changed, so every device row's cached expiry may have moved.
      qc.invalidateQueries({ queryKey: ['asset-devices'] });
      onClose();
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  });

  const datesValid = Boolean(startDate && endDate && endDate >= startDate);

  return (
    <Dialog title="Record a support contract" onClose={onClose} wide={showPicker}>
      <div className="asset-form">
        <label>
          <span>Reference</span>
          <input value={reference} autoFocus
                 onChange={(e) => setReference(e.target.value)}
                 placeholder="The supplier's contract number" />
        </label>
        <label>
          <span>Supplier</span>
          <select value={supplierId} onChange={(e) => setSupplierId(e.target.value)}>
            <option value="">None recorded</option>
            {(suppliers?.items ?? []).map((s) => (
              <option key={s.id} value={s.id}>{s.name}</option>
            ))}
          </select>
        </label>
        <label>
          <span>Kind</span>
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="warranty">Warranty</option>
            <option value="support">Support</option>
            <option value="maintenance">Maintenance</option>
          </select>
        </label>
        <label>
          <span>Service level</span>
          <input value={serviceLevel} placeholder="NBD, 4h onsite, 24x7x4"
                 onChange={(e) => setServiceLevel(e.target.value)} />
        </label>
        <label>
          <span>Starts</span>
          <input type="date" value={startDate}
                 onChange={(e) => setStartDate(e.target.value)} />
        </label>
        <label>
          <span>Ends</span>
          <input type="date" value={endDate}
                 onChange={(e) => setEndDate(e.target.value)} />
        </label>
        <label>
          <span>Cost</span>
          <input type="number" value={cost} min="0" step="0.01"
                 onChange={(e) => setCost(e.target.value)} />
        </label>
        <label>
          <span>Currency</span>
          <input value={currency} maxLength={3} placeholder="USD"
                 onChange={(e) => setCurrency(e.target.value.toUpperCase())} />
        </label>
        <label className="asset-form-wide asset-check">
          <input type="checkbox" checked={autoRenew}
                 onChange={(e) => setAutoRenew(e.target.checked)} />
          <span>Renews automatically</span>
        </label>
        <label className="asset-form-wide">
          <span>Notes</span>
          <textarea rows={2} value={notes}
                    onChange={(e) => setNotes(e.target.value)} />
        </label>

        {!datesValid && (
          <p className="asset-form-error asset-form-wide">
            A contract cannot end before it starts.
          </p>
        )}

        {startDate > today() && (
          // Worth saying at the point of entry rather than leaving somebody to
          // wonder why the asset still reads "no cover".
          <p className="asset-form-note asset-form-wide">
            This contract starts in the future, so it will not count as cover
            until {startDate}.
          </p>
        )}
      </div>

      <div className="asset-form-section">
        <button type="button" onClick={() => setShowPicker((v) => !v)}>
          {showPicker ? 'Hide assets' : 'Choose covered assets'}
        </button>
        <span className="muted" style={{ marginLeft: 10 }}>
          {deviceIds.length ? `${deviceIds.length} selected` : 'None selected'}
        </span>
      </div>

      {showPicker && (
        <DevicePicker selected={deviceIds} onChange={setDeviceIds} />
      )}

      {error && <div className="banner">{error}</div>}

      <DialogActions>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        <button
          type="button"
          disabled={!reference.trim() || !datesValid || create.isPending}
          onClick={() => { setError(null); create.mutate(); }}
        >
          {create.isPending ? 'Saving…' : 'Save contract'}
        </button>
      </DialogActions>
    </Dialog>
  );
}

/** A new supplier, kept small on purpose: the contract form needs one to exist
 *  and nothing else about it is urgent. */
export function SupplierForm({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [accountRef, setAccountRef] = useState('');
  const [contactName, setContactName] = useState('');
  const [contactEmail, setContactEmail] = useState('');
  const [contactPhone, setContactPhone] = useState('');

  const create = useMutation({
    mutationFn: () => api.createSupplier({
      name,
      account_ref: accountRef || null,
      contact_name: contactName || null,
      contact_email: contactEmail || null,
      contact_phone: contactPhone || null,
      notes: null,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['suppliers'] });
      onClose();
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  });

  return (
    <Dialog title="Add a supplier" onClose={onClose}>
      <div className="asset-form">
        <label className="asset-form-wide">
          <span>Name</span>
          <input value={name} autoFocus onChange={(e) => setName(e.target.value)} />
        </label>
        <label>
          <span>Account reference</span>
          <input value={accountRef} onChange={(e) => setAccountRef(e.target.value)} />
        </label>
        <label>
          <span>Contact</span>
          <input value={contactName} onChange={(e) => setContactName(e.target.value)} />
        </label>
        <label>
          <span>Email</span>
          <input type="email" value={contactEmail}
                 onChange={(e) => setContactEmail(e.target.value)} />
        </label>
        <label>
          <span>Phone</span>
          <input value={contactPhone}
                 onChange={(e) => setContactPhone(e.target.value)} />
        </label>
      </div>
      {error && <div className="banner">{error}</div>}
      <DialogActions>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" disabled={!name.trim() || create.isPending}
                onClick={() => { setError(null); create.mutate(); }}>
          {create.isPending ? 'Saving…' : 'Add supplier'}
        </button>
      </DialogActions>
    </Dialog>
  );
}

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function inYears(n: number): string {
  const d = new Date();
  d.setFullYear(d.getFullYear() + n);
  return d.toISOString().slice(0, 10);
}
