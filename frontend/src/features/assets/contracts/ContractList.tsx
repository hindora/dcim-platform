import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type Supplier, type SupportContract } from '../../../api/client';
import { humanise } from '../../../lib/format';
import { ContractForm, SupplierForm } from './ContractForm';

/** Support contracts, soonest expiry first.
 *
 *  That sort is not a default anybody changes: the question this table is
 *  opened to answer is what lapses next. `state` and the "expiring" threshold
 *  both come from the server, so this page cannot disagree with the tile on the
 *  overview about what counts as near.
 */
export function ContractList() {
  const [creating, setCreating] = useState<'contract' | 'supplier' | null>(null);
  const { data, isLoading, error } = useQuery({
    queryKey: ['contracts'],
    queryFn: () => api.contracts({ limit: '500' }),
  });

  const { data: suppliers } = useQuery<{ items: Supplier[] }>({
    queryKey: ['suppliers'],
    queryFn: api.suppliers,
  });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;

  const items = data?.items ?? [];
  const days = data?.expiring_days ?? 90;
  const expired = items.filter((c) => c.state === 'expired');
  const expiring = items.filter((c) => c.state === 'expiring');
  const active = items.filter((c) => c.state === 'active');

  return (
    <>
      <h2>Support</h2>
      <p className="subtitle">
        Cover is a contract, not a date on a machine. One contract covers many
        devices and renews as a unit.
      </p>

      <p className="asset-table-note">
        <button type="button" onClick={() => setCreating('contract')}>
          Record a contract
        </button>
        <button type="button" onClick={() => setCreating('supplier')}>
          Add a supplier
        </button>
      </p>

      {creating === 'contract' && <ContractForm onClose={() => setCreating(null)} />}
      {creating === 'supplier' && <SupplierForm onClose={() => setCreating(null)} />}

      {isLoading && <p className="muted">Loading…</p>}

      {!isLoading && items.length === 0 && (
        <div className="asset-empty">
          No contracts recorded. Until one exists every asset reads “no cover
          recorded”, which is not the same as “out of warranty”.
        </div>
      )}

      {expired.length > 0 && (
        <Table title="Expired" rows={expired} tone="expired" />
      )}
      {expiring.length > 0 && (
        <Table title={`Expiring within ${days} days`} rows={expiring} tone="expiring" />
      )}
      {active.length > 0 && <Table title="Active" rows={active} tone="active" />}

      {suppliers && suppliers.items.length > 0 && (
        <section style={{ marginTop: 28 }}>
          <h3>Suppliers</h3>
          <div className="asset-scroll">
            <table>
              <thead>
                <tr>
                  <th>Supplier</th><th>Account</th><th>Contact</th>
                  <th>Contracts</th><th>Devices</th>
                </tr>
              </thead>
              <tbody>
                {suppliers.items.map((s) => (
                  <tr key={s.id}>
                    <td>{s.name}</td>
                    <td className="asset-tag">
                      {s.account_ref ?? <span className="asset-none">—</span>}
                    </td>
                    <td className="muted">
                      {s.contact_name ?? s.contact_email ?? '—'}
                    </td>
                    <td className="muted">{s.contract_count}</td>
                    <td className="muted">{s.device_count}</td>
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

function Table({ title, rows, tone }: {
  title: string; rows: SupportContract[]; tone: string;
}) {
  return (
    <section style={{ marginBottom: 24 }}>
      <h3>{title} — {rows.length}</h3>
      <div className="asset-scroll">
        <table>
          <thead>
            <tr>
              <th>Reference</th><th>Supplier</th><th>Kind</th>
              <th>Service level</th><th>Ends</th><th>Remaining</th><th>Devices</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.id}>
                <td>
                  <Link to={`/assets/contracts/${c.id}`} className="asset-tag">
                    {c.reference}
                  </Link>
                </td>
                <td className="muted">
                  {c.supplier_name ?? <span className="asset-none">—</span>}
                </td>
                <td className="muted">{humanise(c.kind)}</td>
                <td className="muted">
                  {c.service_level ?? <span className="asset-none">—</span>}
                </td>
                <td className="muted">{c.end_date}</td>
                <td className={`asset-cover is-${tone}`}>
                  {/* Days, not a bar. A countdown to a date is a number, and a
                      gauge would imply a bounded ratio that does not exist. */}
                  {c.days_remaining < 0
                    ? `${Math.abs(c.days_remaining)}d ago`
                    : `${c.days_remaining}d`}
                </td>
                <td className="muted">{c.device_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
