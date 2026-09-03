import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { ApiError, api, type Part, type Store } from '../../../api/client';
import { humanise } from '../../../lib/format';
import { Dialog, DialogActions } from '../components/Dialog';

/** Consumable stock.
 *
 *  Parts are not assets and this page is deliberately not the inventory table.
 *  A part has a count at a place; an asset has identity, a rack and telemetry.
 *  The rule that decides which one something is: if the individual is tracked
 *  it is a device, if only the count is tracked it is a part. A spare SERVER
 *  has a serial and gets racked — it lives in Inventory with lifecycle
 *  "in stock", not here.
 */
export function PartList() {
  const [creating, setCreating] = useState<'part' | 'store' | null>(null);
  const [category, setCategory] = useState('');
  const [search, setSearch] = useState('');
  const [lowOnly, setLowOnly] = useState(false);

  const { data, isLoading, error } = useQuery<{ items: Part[] }>({
    queryKey: ['parts', category, search, lowOnly],
    queryFn: () => api.parts({
      category: category || undefined,
      search: search || undefined,
      below_reorder: lowOnly ? 'true' : undefined,
      limit: '500',
    }),
  });

  const { data: stores } = useQuery<{ items: Store[] }>({
    queryKey: ['stores'],
    queryFn: api.stores,
  });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;

  const items = data?.items ?? [];
  const low = items.filter((p) => p.below_reorder);

  return (
    <>
      <h2>Parts</h2>
      <p className="subtitle">
        Consumables tracked by count, not by serial. Anything you track
        individually — a spare server, a spare switch — is an asset and belongs
        in Inventory.
      </p>

      <p className="asset-table-note">
        <button type="button" onClick={() => setCreating('part')}>New part</button>
        <button type="button" onClick={() => setCreating('store')}>Add a store</button>
      </p>

      {creating === 'part' && (
        <PartForm stores={stores?.items ?? []} onClose={() => setCreating(null)} />
      )}
      {creating === 'store' && <StoreForm onClose={() => setCreating(null)} />}

      <div className="asset-picker-filters" style={{ maxWidth: 620 }}>
        <input type="search" placeholder="SKU or name" value={search}
               onChange={(e) => setSearch(e.target.value)} />
        <select value={category} onChange={(e) => setCategory(e.target.value)}>
          <option value="">All categories</option>
          {['psu', 'fan', 'memory', 'disk', 'optic', 'cable', 'controller',
            'battery', 'filter', 'other'].map((c) => (
              <option key={c} value={c}>{humanise(c)}</option>
          ))}
        </select>
        <label className="asset-check">
          <input type="checkbox" checked={lowOnly}
                 onChange={(e) => setLowOnly(e.target.checked)} />
          <span>Below reorder only</span>
        </label>
      </div>

      {isLoading && <p className="muted">Loading…</p>}

      {!isLoading && items.length === 0 && (
        <div className="asset-empty">
          No parts recorded. Add one, then post a receipt against a store — stock
          only ever moves by a recorded movement, never by typing a number.
        </div>
      )}

      {low.length > 0 && (
        <p className="asset-form-note">
          {low.length} part{low.length === 1 ? ' is' : 's are'} at or below the
          reorder point.
        </p>
      )}

      {items.length > 0 && (
        <div className="asset-scroll">
          <table>
            <thead>
              <tr>
                <th>SKU</th><th>Part</th><th>Category</th><th>Vendor</th>
                <th>On hand</th><th>Reserved</th><th>Reorder at</th>
              </tr>
            </thead>
            <tbody>
              {items.map((p) => (
                <tr key={p.id}>
                  <td>
                    <Link to={`/assets/parts/${p.id}`} className="asset-tag">
                      {p.sku}
                    </Link>
                  </td>
                  <td>{p.name}</td>
                  <td className="muted">{humanise(p.category)}</td>
                  <td className="muted">
                    {p.vendor_name ?? <span className="asset-none">—</span>}
                  </td>
                  <td className={p.below_reorder ? 'asset-shelved' : ''}>
                    {p.on_hand}
                    {/* Icon and label, never colour alone. */}
                    {p.below_reorder && <span title="at or below reorder"> ▾ low</span>}
                  </td>
                  <td className="muted">{p.reserved || '—'}</td>
                  <td className="muted">{p.reorder_at ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {stores && stores.items.length > 0 && (
        <section style={{ marginTop: 26 }}>
          <h3>Stores</h3>
          <div className="asset-scroll">
            <table>
              <thead>
                <tr><th>Store</th><th>Site</th><th>Where</th><th>Lines</th><th>Units</th></tr>
              </thead>
              <tbody>
                {stores.items.map((s) => (
                  <tr key={s.id}>
                    <td>{s.name}</td>
                    <td className="muted">{s.datacenter_code ?? '—'}</td>
                    <td className="muted">
                      {s.room_name ?? s.location_note ?? '—'}
                    </td>
                    <td className="muted">{s.lines}</td>
                    <td className="muted">{s.units}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </>
  );
}

function PartForm({ stores, onClose }: { stores: Store[]; onClose: () => void }) {
  const qc = useQueryClient();
  const [sku, setSku] = useState('');
  const [name, setName] = useState('');
  const [category, setCategory] = useState('psu');
  const [cost, setCost] = useState('');
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () => api.createPart({
      sku, name, category,
      fits_types: [],
      unit_cost: cost ? Number(cost) : null,
      currency: cost ? 'USD' : null,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['parts'] });
      onClose();
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  });

  return (
    <Dialog title="New part" onClose={onClose}>
      <div className="asset-form">
        <label>
          <span>SKU</span>
          <input value={sku} autoFocus placeholder="Manufacturer part number"
                 onChange={(e) => setSku(e.target.value)} />
        </label>
        <label>
          <span>Name</span>
          <input value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label>
          <span>Category</span>
          <select value={category} onChange={(e) => setCategory(e.target.value)}>
            {['psu', 'fan', 'memory', 'disk', 'optic', 'cable', 'controller',
              'battery', 'filter', 'other'].map((c) => (
                <option key={c} value={c}>{humanise(c)}</option>
            ))}
          </select>
        </label>
        <label>
          <span>Unit cost</span>
          <input type="number" min="0" step="0.01" value={cost}
                 onChange={(e) => setCost(e.target.value)} />
        </label>
      </div>
      <p className="asset-form-note">
        {stores.length === 0
          ? 'There are no stores yet. Add one before receiving stock — a count '
            + 'has to be somewhere.'
          : 'Stock is added afterwards by posting a receipt against a store.'}
      </p>
      {error && <div className="banner">{error}</div>}
      <DialogActions>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" disabled={!sku.trim() || !name.trim() || save.isPending}
                onClick={() => { setError(null); save.mutate(); }}>
          {save.isPending ? 'Saving…' : 'Add part'}
        </button>
      </DialogActions>
    </Dialog>
  );
}

function StoreForm({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState('');
  const [note, setNote] = useState('');
  const [dc, setDc] = useState('');
  const [error, setError] = useState<string | null>(null);

  // Derived from rooms, which already carry the site id and code. A store
  // picker does not justify a second endpoint.
  const { data: rooms } = useQuery({
    queryKey: ['rooms'],
    queryFn: api.rooms,
    staleTime: 5 * 60_000,
  });
  const sites = [...new Map((rooms?.items ?? [])
    .filter((r) => r.datacenter_id)
    .map((r) => [r.datacenter_id as string,
                 { id: r.datacenter_id as string, code: r.datacenter_code ?? '—' }]))
    .values()];

  const save = useMutation({
    mutationFn: () => api.createStore({
      name, datacenter_id: dc || null, room_id: null,
      location_note: note || null,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['stores'] });
      onClose();
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  });

  return (
    <Dialog title="Add a store" onClose={onClose}>
      <div className="asset-form">
        <label>
          <span>Name</span>
          <input value={name} autoFocus onChange={(e) => setName(e.target.value)} />
        </label>
        <label>
          <span>Site</span>
          <select value={dc} onChange={(e) => setDc(e.target.value)}>
            <option value="">Offsite / unassigned</option>
            {sites.map((d) => (
              <option key={d.id} value={d.id}>{d.code}</option>
            ))}
          </select>
        </label>
        <label className="asset-form-wide">
          <span>Where exactly</span>
          <input value={note} placeholder="Shelf 3, plant room"
                 onChange={(e) => setNote(e.target.value)} />
        </label>
      </div>
      {error && <div className="banner">{error}</div>}
      <DialogActions>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" disabled={!name.trim() || save.isPending}
                onClick={() => { setError(null); save.mutate(); }}>
          {save.isPending ? 'Saving…' : 'Add store'}
        </button>
      </DialogActions>
    </Dialog>
  );
}
