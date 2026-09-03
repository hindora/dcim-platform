import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import {
  api,
  type AssetFilterOptions,
  type DeviceSummary,
  type Page,
} from '../../../api/client';
import { humanise } from '../../../lib/format';
import { LifecycleChip } from '../components/LifecycleChip';

/** The asset table.
 *
 *  Every filter lives in the URL. That is not tidiness: a filtered view is a
 *  link somebody pastes into a ticket, and that is most of what makes an
 *  inventory screen useful.
 *
 *  A rail rather than chips above the table - there are enough filters that
 *  chips wrap onto three lines and push the first row below the fold.
 */

/** Repeatable params read as arrays; everything else as a single value. */
const MULTI = ['lifecycle', 'category', 'device_type', 'tag'] as const;

export function InventoryTable() {
  const [params, setParams] = useSearchParams();

  const { data: options } = useQuery<AssetFilterOptions>({
    queryKey: ['asset-filter-options'],
    queryFn: api.assetFilterOptions,
    staleTime: 5 * 60_000,
  });

  const query: Record<string, string | string[] | undefined> = { limit: '200' };
  for (const key of MULTI) {
    const all = params.getAll(key);
    if (all.length) query[key] = all;
  }
  for (const key of ['search', 'vendor_id', 'datacenter_id', 'room_id',
                     'rack_id', 'has_serial', 'warranty_state',
                     'owner_group', 'cost_centre']) {
    const v = params.get(key);
    if (v !== null && v !== '') query[key] = v;
  }

  const { data, error, isLoading } = useQuery<Page<DeviceSummary>>({
    queryKey: ['asset-devices', params.toString()],
    queryFn: () => api.assetDevices(query),
    refetchInterval: 30_000,
  });

  function toggleMulti(key: string, value: string) {
    const next = new URLSearchParams(params);
    const current = next.getAll(key);
    next.delete(key);
    const after = current.includes(value)
      ? current.filter((v) => v !== value)
      : [...current, value];
    after.forEach((v) => next.append(key, v));
    setParams(next, { replace: true });
  }

  function setSingle(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  }

  const activeCount = [...params.keys()].length;
  const rows = data?.items ?? [];

  return (
    <>
      <h2>Inventory</h2>
      <p className="subtitle">
        Every asset the platform knows about, whatever state it is in.
      </p>

      <div className="asset-work">
        <aside className="asset-filters" aria-label="Inventory filters">
          <div className="asset-filter-group">
            <label htmlFor="asset-q">Search</label>
            <input
              id="asset-q"
              type="search"
              placeholder="Name, tag, serial, IP"
              value={params.get('search') ?? ''}
              onChange={(e) => setSingle('search', e.target.value)}
            />
          </div>

          <div className="asset-filter-group">
            <p className="legend">Lifecycle</p>
            {(options?.lifecycles ?? []).map((life) => (
              <label className="asset-check" key={life.value}>
                <input
                  type="checkbox"
                  checked={params.getAll('lifecycle').includes(life.value)}
                  onChange={() => toggleMulti('lifecycle', life.value)}
                />
                {life.label}
              </label>
            ))}
          </div>

          <div className="asset-filter-group">
            <label htmlFor="asset-type">Type</label>
            <select
              id="asset-type"
              value={params.get('device_type') ?? ''}
              onChange={(e) => setSingle('device_type', e.target.value)}
            >
              <option value="">All types</option>
              {(options?.device_types ?? [])
                .filter((t) => t.device_count > 0)
                .map((t) => (
                  <option key={t.code} value={t.code}>
                    {t.display_name} ({t.device_count})
                  </option>
                ))}
            </select>
          </div>

          <div className="asset-filter-group">
            <label htmlFor="asset-vendor">Vendor</label>
            <select
              id="asset-vendor"
              value={params.get('vendor_id') ?? ''}
              onChange={(e) => setSingle('vendor_id', e.target.value)}
            >
              <option value="">Any vendor</option>
              {(options?.vendors ?? []).map((v) => (
                <option key={v.id} value={v.id}>
                  {v.name} ({v.device_count})
                </option>
              ))}
            </select>
          </div>

          <div className="asset-filter-group">
            <label htmlFor="asset-identity">Identity</label>
            <select
              id="asset-identity"
              value={params.get('has_serial') ?? ''}
              onChange={(e) => setSingle('has_serial', e.target.value)}
            >
              <option value="">Any</option>
              <option value="true">Has a serial</option>
              <option value="false">No serial — needs reconciling</option>
            </select>
          </div>

          <div className="asset-filter-group">
            <label htmlFor="asset-cover">Cover</label>
            <select
              id="asset-cover"
              value={params.get('warranty_state') ?? ''}
              onChange={(e) => setSingle('warranty_state', e.target.value)}
            >
              <option value="">Any</option>
              <option value="expired">Expired</option>
              <option value="expiring">Expiring soon</option>
              <option value="active">Covered</option>
              <option value="unknown">No cover recorded</option>
            </select>
          </div>

          {activeCount > 0 && (
            <button
              type="button"
              className="asset-filter-clear"
              onClick={() => setParams(new URLSearchParams(), { replace: true })}
            >
              Clear {activeCount} filter{activeCount === 1 ? '' : 's'}
            </button>
          )}
        </aside>

        <div>
          {error && <div className="banner">Failed to load: {String(error)}</div>}

          <p className="asset-table-note">
            <span>
              {isLoading ? 'Loading…' : `${rows.length.toLocaleString()} assets`}
            </span>
            {data?.next_cursor && (
              <span className="muted">
                More beyond this page — narrow the filters to see them all.
              </span>
            )}
          </p>

          <div className="asset-scroll">
            <table>
              <thead>
                <tr>
                  <th>Asset tag</th>
                  <th>Name</th>
                  <th>Type</th>
                  <th>Model</th>
                  <th>Location</th>
                  <th>Lifecycle</th>
                  <th>Cover</th>
                  <th>Serial</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((d) => (
                  <tr key={d.id}>
                    <td className="asset-tag">
                      {d.asset_tag ?? <span className="asset-none">—</span>}
                    </td>
                    <td>
                      <Link to={`/assets/inventory/${d.id}`}>{d.name}</Link>
                    </td>
                    <td className="muted">{humanise(d.device_type)}</td>
                    <td className="muted">{d.model ?? '—'}</td>
                    <td className="muted">
                      {[d.location.datacenter_code, d.location.room_name,
                        d.location.rack_name,
                        d.location.u_start ? `U${d.location.u_start}` : null]
                        .filter(Boolean).join(' · ') || '—'}
                    </td>
                    <td><LifecycleChip state={d.lifecycle} /></td>
                    <td>
                      {d.warranty_state && d.warranty_state !== 'unknown' ? (
                        <span className={`asset-cover is-${d.warranty_state}`}>
                          {d.warranty_expires}
                        </span>
                      ) : (
                        <span className="asset-none">—</span>
                      )}
                    </td>
                    <td className="asset-tag">
                      {d.serial_number ?? <span className="asset-none">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {!isLoading && rows.length === 0 && (
            <div className="asset-empty">
              {activeCount > 0 ? (
                <>
                  <p>No assets match these filters.</p>
                  <button
                    type="button"
                    onClick={() => setParams(new URLSearchParams(), { replace: true })}
                  >
                    Clear filters
                  </button>
                </>
              ) : (
                <p>
                  The inventory is empty. Run the seed importer to populate it
                  from the topology export.
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
