import { useState } from 'react';
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
import { BulkBar } from './BulkBar';
import { ImportDialog } from './ImportDialog';
import { Pagination } from './Pagination';

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
  const [selected, setSelected] = useState<string[]>([]);
  const [importing, setImporting] = useState(false);

  // Page size AND page number live in the URL with the filters. Offset paging
  // makes the page number sufficient to locate a page, so a pasted link now
  // reproduces the exact view - which a cursor never could.
  const pageSize = Number(params.get('page_size') || 50);
  const page = Math.max(1, Number(params.get('page') || 1));

  const { data: options } = useQuery<AssetFilterOptions>({
    queryKey: ['asset-filter-options'],
    queryFn: api.assetFilterOptions,
    staleTime: 5 * 60_000,
  });

  const query: Record<string, string | string[] | undefined> = {
    limit: String(pageSize),
    // The denominator behind "1-50 of 664", and what makes the last page
    // reachable at all. Asked for only by this screen; a plain next-page fetch
    // elsewhere does not pay for the count.
    with_total: 'true',
  };
  if (page > 1) query.offset = String((page - 1) * pageSize);
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

  // page_size is a view preference, not a filter - counting it would make the
  // "clear filters" button claim there is one when there is not.
  const VIEW_KEYS = new Set(['page_size', 'page']);
  const activeCount = [...params.keys()].filter((k) => !VIEW_KEYS.has(k)).length;

  function goToPage(next: number) {
    const q = new URLSearchParams(params);
    if (next <= 1) q.delete('page');
    else q.set('page', String(next));
    setParams(q, { replace: true });
    // The selection was of rows that are about to leave the screen. Carrying it
    // invisibly across a page turn is how somebody decommissions the wrong
    // forty devices.
    setSelected([]);
  }

  function setPageSize(size: number) {
    const next = new URLSearchParams(params);
    if (size === 50) next.delete('page_size');
    else next.set('page_size', String(size));
    // Row boundaries move, so the current page number means something else.
    next.delete('page');
    setParams(next, { replace: true });
    setSelected([]);
  }
  const rows = data?.items ?? [];
  const chosen = new Set(selected);
  const allShown = rows.length > 0 && rows.every((d) => chosen.has(d.id));
  const someShown = rows.some((d) => chosen.has(d.id));

  // Only transitions EVERY selected asset can make. Offering a move that some
  // of them would refuse turns one action into a report nobody wanted.
  const states = [...new Set(rows.filter((d) => chosen.has(d.id))
    .map((d) => d.lifecycle ?? 'in_service'))];
  const allowed = states.length === 1
    ? (options?.lifecycles ?? [])
        .map((l) => l.value)
        .filter((v) => v !== states[0])
    : [];

  return (
    <>
      <h2>Inventory</h2>
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

          <BulkBar selected={selected} allowed={allowed}
                   onClear={() => setSelected([])}>
            <span className="muted">{isLoading ? 'Loading…' : ''}</span>
            <span style={{ flex: 1 }} />
            <a href={`/api/v1/assets/bulk/export?${params.toString()}`}
               download="assets.csv">Export CSV</a>
            <button type="button" onClick={() => setImporting(true)}>
              Import CSV
            </button>
          </BulkBar>

          <div className="asset-scroll">
            <table>
              <thead>
                <tr>
                  <th className="asset-select-head">
                    {/* Labelled, because an unexplained column of checkboxes
                        reads as decoration. Clicking the word toggles too - a
                        14px box is a small target to ask for repeatedly. */}
                    <label title={`Select all ${rows.length} on this page`}>
                      <input
                        type="checkbox"
                        checked={allShown}
                        ref={(el) => {
                          // Some-but-not-all. Without it the box reads as
                          // "nothing selected" while a bulk action is armed on
                          // rows scrolled out of sight.
                          if (el) el.indeterminate = someShown && !allShown;
                        }}
                        aria-label={`Select all ${rows.length} assets on this page`}
                        onChange={() => setSelected(allShown
                          ? selected.filter((id) => !rows.some((d) => d.id === id))
                          : [...new Set([...selected, ...rows.map((d) => d.id)])])}
                      />
                      <span>Select</span>
                    </label>
                  </th>
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
                  <tr key={d.id} className={chosen.has(d.id) ? 'is-selected' : ''}>
                    <td>
                      <input
                        type="checkbox"
                        checked={chosen.has(d.id)}
                        aria-label={`Select ${d.name}`}
                        onChange={() => setSelected(chosen.has(d.id)
                          ? selected.filter((x) => x !== d.id)
                          : [...selected, d.id])}
                      />
                    </td>
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

          {!isLoading && (rows.length > 0 || page > 1) && (
            <Pagination
              page={page}
              pageSize={pageSize}
              shown={rows.length}
              total={data?.total}
              hasNext={Boolean(data?.next_cursor)}
              onPage={goToPage}
              onSize={setPageSize}
            />
          )}

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
                <p>The inventory is empty.</p>
              )}
            </div>
          )}
        </div>
      </div>
      {importing && <ImportDialog onClose={() => setImporting(false)} />}
    </>
  );
}
