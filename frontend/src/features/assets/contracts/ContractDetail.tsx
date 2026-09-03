import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { api, type SupportContract } from '../../../api/client';
import { humanise } from '../../../lib/format';

/** One contract and everything it covers. */
export function ContractDetail() {
  const { id = '' } = useParams();

  const { data, isLoading, error } = useQuery<SupportContract>({
    queryKey: ['contract', id],
    queryFn: () => api.contract(id),
    enabled: Boolean(id),
  });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (isLoading || !data) return <p className="muted">Loading…</p>;

  return (
    <>
      <p className="asset-table-note">
        <Link to="/assets/contracts">← Support</Link>
      </p>

      <div className="asset-record-head">
        <h2>{data.reference}</h2>
        <span className={`asset-life is-${data.state}`}>{humanise(data.state)}</span>
      </div>

      <div className="asset-facts" style={{ marginBottom: 20 }}>
        <div className="asset-fact">
          <div className="k">Supplier</div>
          <div className="v">{data.supplier_name ?? '—'}</div>
        </div>
        <div className="asset-fact">
          <div className="k">Kind</div><div className="v">{humanise(data.kind)}</div>
        </div>
        <div className="asset-fact">
          <div className="k">Service level</div>
          <div className="v">{data.service_level ?? '—'}</div>
        </div>
        <div className="asset-fact">
          <div className="k">Runs</div>
          <div className="v">{data.start_date} → {data.end_date}</div>
        </div>
        <div className="asset-fact">
          <div className="k">Remaining</div>
          <div className={`v asset-cover is-${data.state}`}>
            {/* A sentence, not a gauge: this is a date, not a bounded ratio. */}
            {data.days_remaining < 0
              ? `expired ${Math.abs(data.days_remaining)} days ago`
              : `${data.days_remaining} days`}
          </div>
        </div>
        <div className="asset-fact">
          <div className="k">Renewal</div>
          <div className="v">{data.auto_renew ? 'Automatic' : 'Manual'}</div>
        </div>
        {data.cost != null && (
          <div className="asset-fact">
            <div className="k">Cost</div>
            <div className="v">{data.cost} {data.currency ?? ''}</div>
          </div>
        )}
      </div>

      {data.notes && <p className="muted">{data.notes}</p>}

      <h3>Covered assets — {data.devices?.length ?? data.device_count}</h3>
      {data.devices && data.devices.length > 0 ? (
        <div className="asset-scroll">
          <table>
            <thead>
              <tr><th>Asset</th><th>Type</th><th>Serial</th><th>Covered until</th></tr>
            </thead>
            <tbody>
              {data.devices.map((d) => (
                <tr key={d.id}>
                  <td><Link to={`/assets/inventory/${d.id}`}>{d.name}</Link></td>
                  <td className="muted">{humanise(d.device_type)}</td>
                  <td className="asset-tag">
                    {d.serial_number ?? <span className="asset-none">—</span>}
                  </td>
                  <td className="muted">
                    {/* This asset's cover may run past THIS contract: another
                        one may cover it for longer, and the column is the
                        latest of them. */}
                    {d.warranty_expires ?? <span className="asset-none">—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">No assets covered.</p>
      )}
    </>
  );
}
