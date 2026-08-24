import { useMemo, useState } from 'react';

/** Scope, search, drill-down and paging - the state every estate page shares.
 *
 *  Drilling into a site does not refetch. Both scopes arrive in one payload,
 *  so selecting a site is a filter over rows already in hand: the table cannot
 *  show a site total measured at one instant beside room rows measured at
 *  another, which is exactly what a second request would produce.
 */
export interface EstateRowLike {
  id: string;
  name: string;
  site_id: string;
  site_code: string;
  site_name: string;
  floor?: string | null;
}

export function useEstateTable<Row extends EstateRowLike>(
  sites: Row[], rooms: Row[],
) {
  const [scope, setScope] = useState<'sites' | 'rooms'>('sites');
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<Row | null>(null);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(25);

  const base = useMemo(() => {
    if (selected) return rooms.filter((r) => r.site_id === selected.site_id);
    return scope === 'sites' ? sites : rooms;
  }, [scope, selected, sites, rooms]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return base;
    return base.filter((r) =>
      `${r.name} ${r.site_code} ${r.site_name} ${r.floor ?? ''}`.toLowerCase().includes(q));
  }, [base, search]);

  const current = Math.min(page, Math.max(0, Math.ceil(filtered.length / pageSize) - 1));
  const visible = filtered.slice(current * pageSize, (current + 1) * pageSize);

  function drillInto(row: Row) {
    setSelected(row);
    setPage(0);
  }

  function clearDrill() {
    setSelected(null);
    setPage(0);
  }

  return {
    scope,
    setScope: (s: 'sites' | 'rooms') => { setScope(s); setSelected(null); setPage(0); },
    search, setSearch: (v: string) => { setSearch(v); setPage(0); },
    selected, drillInto, clearDrill,
    /** Everything matching the filter - what CSV export uses, not just the page. */
    filtered,
    visible,
    page: current, setPage,
    pageSize, setPageSize,
  };
}
