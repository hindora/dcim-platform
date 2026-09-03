import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import {
  ApiError,
  api,
  type Part,
  type StockMovement,
  type Store,
} from '../../../api/client';
import { humanise, relativeTime } from '../../../lib/format';
import { Dialog, DialogActions } from '../components/Dialog';

/** One part: where it is, and every movement that got it there.
 *
 *  There is no editable quantity field anywhere on this page, and that is
 *  deliberate. Correcting a count is posting an adjustment with a note, which
 *  leaves a record of the correction — an inventory whose numbers can be
 *  silently overwritten is a spreadsheet.
 */
export function PartDetail() {
  const { id = '' } = useParams();
  const [moving, setMoving] = useState(false);

  const { data, isLoading, error } = useQuery<Part>({
    queryKey: ['part', id],
    queryFn: () => api.part(id),
    enabled: Boolean(id),
  });

  const { data: ledger } = useQuery<{ items: StockMovement[] }>({
    queryKey: ['part-movements', id],
    queryFn: () => api.partMovements(id),
    enabled: Boolean(id),
  });

  const { data: stores } = useQuery<{ items: Store[] }>({
    queryKey: ['stores'],
    queryFn: api.stores,
  });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (isLoading || !data) return <p className="muted">Loading…</p>;

  return (
    <>
      <p className="asset-table-note">
        <Link to="/assets/parts">← Parts</Link>
      </p>

      <div className="asset-record-head">
        <h2>{data.name}</h2>
        <span className="asset-tag">{data.sku}</span>
        {data.below_reorder && (
          <span className="asset-life is-expiring">Below reorder</span>
        )}
      </div>

      <div className="asset-facts" style={{ marginBottom: 20 }}>
        <Fact k="Category" v={humanise(data.category)} />
        <Fact k="Vendor" v={data.vendor_name} />
        <Fact k="On hand" v={String(data.on_hand)} />
        <Fact k="Reserved" v={String(data.reserved)} />
        <Fact k="Unit cost"
              v={data.unit_cost != null
                ? `${data.unit_cost} ${data.currency ?? ''}`.trim() : null} />
      </div>

      <p className="asset-table-note">
        <button type="button" onClick={() => setMoving(true)}>Post a movement</button>
      </p>

      {moving && (
        <MovementForm partId={id} stores={stores?.items ?? []}
                      onClose={() => setMoving(false)} />
      )}

      <h3>Stock by store</h3>
      {data.stock && data.stock.length > 0 ? (
        <div className="asset-scroll">
          <table>
            <thead>
              <tr>
                <th>Store</th><th>Site</th><th>On hand</th>
                <th>Reserved</th><th>Available</th><th>Reorder at</th>
              </tr>
            </thead>
            <tbody>
              {data.stock.map((s) => (
                <tr key={s.store_id}>
                  <td>{s.store_name}</td>
                  <td className="muted">{s.datacenter_code ?? '—'}</td>
                  <td>{s.on_hand}</td>
                  <td className="muted">{s.reserved || '—'}</td>
                  <td className="muted">{s.available}</td>
                  <td className="muted">{s.reorder_at ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">Nothing on hand anywhere.</p>
      )}

      <h3>Movement ledger</h3>
      {ledger && ledger.items.length > 0 ? (
        <div className="asset-scroll">
          <table>
            <thead>
              <tr>
                <th>When</th><th>Change</th><th>Reason</th><th>Store</th>
                <th>Fitted to</th><th>By</th><th>Note</th>
              </tr>
            </thead>
            <tbody>
              {ledger.items.map((m) => (
                <tr key={m.id}>
                  <td className="muted" title={m.ts}>{relativeTime(m.ts)}</td>
                  <td className={m.delta < 0 ? 'asset-cover is-expired' : ''}>
                    {m.delta > 0 ? `+${m.delta}` : m.delta}
                  </td>
                  <td className="muted">{humanise(m.reason)}</td>
                  <td className="muted">{m.store_name}</td>
                  <td className="muted">
                    {m.device_id
                      ? <Link to={`/assets/inventory/${m.device_id}`}>
                          {m.device_name}
                        </Link>
                      : <span className="asset-none">—</span>}
                  </td>
                  <td className="muted">{m.actor}</td>
                  <td className="muted">
                    {m.note ?? <span className="asset-none">—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">Nothing has moved yet.</p>
      )}
    </>
  );
}

function MovementForm({ partId, stores, onClose }: {
  partId: string; stores: Store[]; onClose: () => void;
}) {
  const qc = useQueryClient();
  const [storeId, setStoreId] = useState(stores[0]?.id ?? '');
  const [reason, setReason] = useState('receipt');
  const [qty, setQty] = useState('1');
  const [note, setNote] = useState('');
  const [error, setError] = useState<string | null>(null);

  // Receipt adds, everything else takes away. The operator types a positive
  // quantity and the direction comes from the reason, because "how many" and
  // "which way" are two different questions and a signed box asks them at once.
  const outward = reason !== 'receipt';
  const noteRequired = reason === 'adjustment';

  const post = useMutation({
    mutationFn: () => api.postMovement(partId, {
      store_id: storeId,
      delta: outward ? -Math.abs(Number(qty)) : Math.abs(Number(qty)),
      reason,
      note: note || undefined,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['part', partId] });
      qc.invalidateQueries({ queryKey: ['part-movements', partId] });
      qc.invalidateQueries({ queryKey: ['parts'] });
      qc.invalidateQueries({ queryKey: ['asset-summary'] });
      onClose();
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  });

  return (
    <Dialog title="Post a stock movement" onClose={onClose}>
      <div className="asset-form">
        <label>
          <span>Store</span>
          <select value={storeId} onChange={(e) => setStoreId(e.target.value)}>
            {stores.map((s) => (
              <option key={s.id} value={s.id}>
                {s.datacenter_code ? `${s.datacenter_code} · ` : ''}{s.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Reason</span>
          <select value={reason} onChange={(e) => setReason(e.target.value)}>
            <option value="receipt">Receipt — stock arriving</option>
            <option value="consumed">Consumed — fitted to equipment</option>
            <option value="adjustment">Adjustment — physical count differs</option>
            <option value="rma">RMA — returned to the supplier</option>
            <option value="transfer">Transfer — out to another store</option>
          </select>
        </label>
        <label>
          <span>Quantity</span>
          <input type="number" min="1" step="1" value={qty}
                 onChange={(e) => setQty(e.target.value)} />
        </label>
        <label className="asset-form-wide">
          <span>Note {noteRequired && '(required)'}</span>
          <input value={note} onChange={(e) => setNote(e.target.value)}
                 placeholder={noteRequired
                   ? 'What the physical count found, and why it differs'
                   : 'Optional'} />
        </label>
      </div>

      <p className="asset-form-note">
        {outward
          ? `This will take ${Math.abs(Number(qty) || 0)} out of stock.`
          : `This will add ${Math.abs(Number(qty) || 0)} to stock.`}
        {noteRequired && ' An adjustment overrides the ledger with a physical '
          + 'count, so it has to say why.'}
      </p>

      {error && <div className="banner">{error}</div>}

      <DialogActions>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        <button
          type="button"
          disabled={!storeId || !Number(qty) || post.isPending
                    || (noteRequired && !note.trim())}
          onClick={() => { setError(null); post.mutate(); }}
        >
          {post.isPending ? 'Posting…' : 'Post movement'}
        </button>
      </DialogActions>
    </Dialog>
  );
}

function Fact({ k, v }: { k: string; v?: string | null }) {
  return (
    <div className="asset-fact">
      <div className="k">{k}</div>
      <div className="v">{v ?? <span className="asset-none">—</span>}</div>
    </div>
  );
}
