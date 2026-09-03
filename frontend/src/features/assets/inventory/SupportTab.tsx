import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  api,
  type DeviceDetail,
  type SupportContract,
} from '../../../api/client';
import { humanise } from '../../../lib/format';

/** What this asset cost, who it came from, and what still covers it. */
export function SupportTab({ device }: { device: DeviceDetail }) {
  const { data, isLoading } = useQuery<{ items: SupportContract[] }>({
    queryKey: ['device-contracts', device.id],
    queryFn: () => api.deviceContracts(device.id),
  });

  const contracts = data?.items ?? [];

  return (
    <>
      <div className="asset-panel" style={{ marginBottom: 18 }}>
        <h3>Cover</h3>
        {/* A sentence, not a gauge. Gauges are for bounded ratios; this is a
            date, and "554 days" is what somebody actually needs to hear. */}
        <p className={`asset-cover-line is-${device.warranty_state ?? 'unknown'}`}>
          {coverSentence(device)}
        </p>
        {contracts.length === 0 && (
          <p className="muted">
            No contract covers this asset. That is not the same as out of
            warranty — it means nobody has recorded one.
          </p>
        )}
      </div>

      <div className="asset-facts" style={{ marginBottom: 20 }}>
        <Fact k="Owner" v={device.owner_group} />
        <Fact k="Cost centre" v={device.cost_centre} />
      </div>

      <h3>Contracts</h3>
      {isLoading && <p className="muted">Loading…</p>}
      {contracts.length > 0 && (
        <div className="asset-scroll">
          <table>
            <thead>
              <tr>
                <th>Reference</th><th>Supplier</th><th>Kind</th>
                <th>Service level</th><th>Ends</th><th>State</th>
              </tr>
            </thead>
            <tbody>
              {contracts.map((c) => (
                <tr key={c.id}>
                  <td>
                    <Link to={`/assets/contracts/${c.id}`} className="asset-tag">
                      {c.reference}
                    </Link>
                  </td>
                  <td className="muted">{c.supplier_name ?? '—'}</td>
                  <td className="muted">{humanise(c.kind)}</td>
                  <td className="muted">{c.service_level ?? '—'}</td>
                  <td className="muted">{c.end_date}</td>
                  <td>
                    <span className={`asset-life is-${c.state}`}>
                      {humanise(c.state)}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function coverSentence(device: DeviceDetail): string {
  if (!device.warranty_expires) return 'No cover recorded.';
  const days = Math.round(
    (new Date(device.warranty_expires).getTime() - Date.now()) / 86_400_000);
  const when = new Date(device.warranty_expires).toLocaleDateString(undefined, {
    day: 'numeric', month: 'long', year: 'numeric',
  });
  if (days < 0) return `Cover expired ${Math.abs(days)} days ago, on ${when}.`;
  return `Covered until ${when} — ${days} days.`;
}

function Fact({ k, v }: { k: string; v?: string | null }) {
  return (
    <div className="asset-fact">
      <div className="k">{k}</div>
      <div className="v">{v ?? <span className="asset-none">—</span>}</div>
    </div>
  );
}
