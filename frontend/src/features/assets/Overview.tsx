import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type AssetSummary } from '../../api/client';
import { humanise } from '../../lib/format';

/** The asset landing page: a stock-take, not a trend.
 *
 *  Nothing here paginates and nothing here is a time series. The question is
 *  what do we own, where is it, and what do we not know about it - and every
 *  answer is a count from one call (docs/21 §3).
 */

/* Seven states, in the enum's own order so the stacked bar reads as a
   progression. `installed` and `in_service` are deliberately different hues:
   the whole reason `installed` exists is that it is racked and must NOT alarm,
   and a chart that paints it as live hides exactly that. */
const LIFECYCLE_HUE: Record<string, string> = {
  planned: 'var(--accent-dim)',
  in_stock: 'var(--text-faint)',
  installed: 'var(--accent)',
  in_service: 'var(--ok)',
  maintenance: 'var(--warn)',
  decommissioned: 'var(--unknown)',
  retired: 'var(--text-faint)',
};

const LIFECYCLE_ORDER = ['planned', 'in_stock', 'installed', 'in_service',
                         'maintenance', 'decommissioned', 'retired'];

export function AssetOverview() {
  const { data, error, isLoading } = useQuery<AssetSummary>({
    queryKey: ['asset-summary'],
    queryFn: api.assetSummary,
    refetchInterval: 60_000,
  });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;

  if (isLoading || !data) {
    return (
      <>
        <h2>Overview</h2>
        <div className="asset-tiles">
          {[0, 1, 2, 3].map((i) => (
            <div className="asset-tile" key={i}>
              <div className="asset-skeleton" style={{ width: '60%' }} />
              <div className="asset-skeleton" style={{ height: 28, marginTop: 10 }} />
            </div>
          ))}
        </div>
      </>
    );
  }

  const { totals, identity, estate, by_category: byCategory, discovery } = data;
  const placed = estate.u_used + estate.u_reserved;
  const maxCategory = Math.max(1, ...byCategory.map((c) => c.n));

  return (
    <>
      <h2>Overview</h2>
      <p className="subtitle">
        The estate as an asset base. Counts are live; nothing here is a
        measurement, so nothing here needs a time range.
      </p>

      <div className="asset-tiles">
        <Link className="asset-tile" to="/assets/inventory">
          <div className="k">Assets</div>
          <div className="v">{totals.assets.toLocaleString()}</div>
          <div className="sub">across {estate.datacenters} sites</div>
        </Link>

        <Link className="asset-tile" to="/assets/inventory?lifecycle=in_service">
          <div className="k">In service</div>
          <div className="v">{totals.in_service.toLocaleString()}</div>
          <div className="sub">
            {totals.maintenance} in maintenance · {totals.planned} planned
          </div>
        </Link>

        {/* Reads the whole estate today, and that is the point: docs/19 B2 put
            where somebody sees it. When the importer carries serials this falls
            to zero and stops being interesting, which is what an instrument
            should do. */}
        <Link
          className={`asset-tile${identity.unidentified ? ' is-gap' : ''}`}
          to="/assets/inventory?has_serial=false"
        >
          <div className="k">Unidentified</div>
          <div className="v">{identity.unidentified.toLocaleString()}</div>
          <div className="sub">
            {identity.unidentified
              ? 'no serial and no asset tag — cannot be reconciled'
              : 'every asset carries an identity'}
          </div>
        </Link>

        {/* Rendered only when migration 0047 has run. Before it the block is
            ABSENT from the payload, and a tile reading "0 expiring" with no
            contract table is a false statement an operator would act on. */}
        {data.warranty && (
          <Link
            className={`asset-tile${data.warranty.expired ? ' is-gap' : ''}`}
            to="/assets/inventory?warranty_state=expiring"
          >
            <div className="k">Cover expiring</div>
            <div className="v">{data.warranty.expiring.toLocaleString()}</div>
            <div className="sub">
              {data.warranty.expired
                ? `${data.warranty.expired} already expired`
                : `within ${data.expiring_days ?? 90} days`}
              {data.warranty.unknown
                ? ` · ${data.warranty.unknown} with no cover recorded`
                : ''}
            </div>
          </Link>
        )}

        <Link className="asset-tile" to="/assets/estate">
          <div className="k">Rack space free</div>
          <div className="v">{estate.u_free.toLocaleString()}U</div>
          <div className="sub">
            of {estate.u_total.toLocaleString()}U in {estate.racks} racks
          </div>
        </Link>
      </div>

      <div className="asset-cols">
        <div className="asset-panel">
          <h3>By category</h3>
          {byCategory.length === 0 ? (
            <p className="muted">Nothing classified yet.</p>
          ) : (
            <div className="asset-barlist">
              {byCategory.map((row) => (
                <div className="asset-barlist-row" key={row.category}>
                  <Link to={`/assets/inventory?category=${encodeURIComponent(row.category)}`}>
                    {humanise(row.category)}
                  </Link>
                  <div className="asset-bar">
                    <span style={{ width: `${(row.n / maxCategory) * 100}%` }} />
                  </div>
                  <div className="n">{row.n.toLocaleString()}</div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="asset-panel">
          <h3>By lifecycle</h3>
          {/* One stacked bar, because the states partition the estate. Separate
              bars invite reading each against the wrong denominator. */}
          <div className="asset-stack">
            {LIFECYCLE_ORDER.map((state) => {
              const n = (totals as unknown as Record<string, number>)[state] ?? 0;
              if (!n) return null;
              return (
                <span
                  key={state}
                  title={`${humanise(state)}: ${n}`}
                  style={{
                    width: `${(n / Math.max(1, totals.assets)) * 100}%`,
                    background: LIFECYCLE_HUE[state],
                  }}
                />
              );
            })}
          </div>
          <div className="asset-legend">
            {LIFECYCLE_ORDER.map((state) => {
              const n = (totals as unknown as Record<string, number>)[state] ?? 0;
              if (!n) return null;
              return (
                <span key={state}>
                  <span className="sw" style={{ background: LIFECYCLE_HUE[state] }} />
                  {humanise(state)} {n.toLocaleString()}
                </span>
              );
            })}
          </div>
        </div>

        <div className="asset-panel">
          <h3>Needs attention</h3>
          <ul className="asset-attention">
            {identity.unidentified > 0 && (
              <li>
                <Link to="/assets/inventory?has_serial=false">
                  {identity.unidentified.toLocaleString()} assets carry no serial
                </Link>
                {' — '}reconciliation cannot match them
              </li>
            )}
            {data.contracts && data.contracts.expired > 0 && (
              <li>
                <Link to="/assets/contracts">
                  {data.contracts.expired} contracts have expired
                </Link>
                {' — '}assets under them have no cover
              </li>
            )}
            {data.contracts && data.contracts.expiring > 0 && (
              <li>
                <Link to="/assets/contracts">
                  {data.contracts.expiring} contracts expiring
                </Link>
                {' '}within {data.expiring_days ?? 90} days
              </li>
            )}
            {discovery.new_candidates > 0 && (
              <li>
                <Link to="/assets/discovery">
                  {discovery.new_candidates} discovery candidates
                </Link>
                {' '}awaiting a decision
              </li>
            )}
            {identity.unidentified === 0 && discovery.new_candidates === 0
              && !data.contracts?.expired && !data.contracts?.expiring && (
              <li className="muted">Nothing needs attention.</li>
            )}
          </ul>
          <p className="muted" style={{ marginTop: 12, fontSize: '0.78rem' }}>
            Stock queues appear here once parts have a table. They are absent
            rather than reading zero.
          </p>
        </div>
      </div>

      <div className="asset-panel" style={{ marginTop: 20 }}>
        <h3>Rack space</h3>
        <div className="asset-stack">
          <span
            title={`Used ${estate.u_used}U`}
            style={{ width: `${(estate.u_used / Math.max(1, estate.u_total)) * 100}%`,
                     background: 'var(--accent)' }}
          />
          <span
            title={`Held ${estate.u_reserved}U`}
            style={{ width: `${(estate.u_reserved / Math.max(1, estate.u_total)) * 100}%`,
                     background: 'var(--warn)' }}
          />
        </div>
        <div className="asset-legend">
          <span>
            <span className="sw" style={{ background: 'var(--accent)' }} />
            Used {estate.u_used.toLocaleString()}U
          </span>
          <span>
            <span className="sw" style={{ background: 'var(--warn)' }} />
            Held for planned {estate.u_reserved.toLocaleString()}U
          </span>
          <span>
            <span className="sw" style={{ background: 'var(--bg-inset)' }} />
            Free {estate.u_free.toLocaleString()}U
          </span>
          <span className="muted">
            {estate.rooms} rooms · {estate.racks} racks · {placed}U placed
          </span>
        </div>
      </div>
    </>
  );
}
