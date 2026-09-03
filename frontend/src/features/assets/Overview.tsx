import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type AssetSummary } from '../../api/client';
import { Charts } from './Charts';

/** The asset landing page: a stock-take, not a trend.
 *
 *  Nothing here paginates and nothing here is a time series. The question is
 *  what do we own, where is it, and what do we not know about it - and every
 *  answer is a count from one call (docs/21 §3).
 */

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

  const { totals, identity, estate, discovery } = data;
  return (
    <>
      <h2>Overview</h2>
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
            {data.stock && data.stock.below_reorder > 0 && (
              <li>
                <Link to="/assets/parts?below_reorder=true">
                  {data.stock.below_reorder} parts below their reorder point
                </Link>
              </li>
            )}
            {data.reservations && data.reservations.overdue > 0 && (
              <li>
                <Link to="/assets/reservations">
                  {data.reservations.overdue} reservations past their expiry
                </Link>
                {' — '}holding space nobody has claimed
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
              && !data.contracts?.expired && !data.contracts?.expiring
              && !data.stock?.below_reorder && !data.reservations?.overdue && (
              <li className="muted">Nothing needs attention.</li>
            )}
          </ul>

        </div>
      </div>

      <Charts />

    </>
  );
}
