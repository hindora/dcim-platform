import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

/** Scope, search, drill-down and paging - the state every estate page shares.
 *
 *  Drilling into a site does not refetch. Both scopes arrive in one payload,
 *  so selecting a site is a filter over rows already in hand: the table cannot
 *  show a site total measured at one instant beside room rows measured at
 *  another, which is exactly what a second request would produce.
 *
 *  Where the reader is - scope, site, room - lives in the URL (?scope=rooms,
 *  ?site=, ?room=), not in component memory. A rack row opens another page;
 *  coming back through history must land on the drilled-in table the reader
 *  left, and a drilled-in view must survive being sent to a colleague.
 *  Search and page stay local: nobody wants a shared link to page three.
 */
export interface EstateRowLike {
  id: string;
  kind?: 'site' | 'room' | 'rack';
  name: string;
  /** Rack rows: the room they stand in. */
  room_id?: string;
  site_id: string;
  site_code: string;
  site_name: string;
  floor?: string | null;
  room_class?: string | null;
}

/** `racks` is the optional third tier. When it is supplied, drilling into a
 *  room shows that room's racks; when it is not, a room is the floor and
 *  the page decides what a room click means. Same payload rule as sites:
 *  racks arrive with the rooms, so no tier is measured at a different
 *  instant from the one above it. */
export function useEstateTable<Row extends EstateRowLike>(
  sites: Row[], rooms: Row[], racks?: Row[],
) {
  const [params, setParams] = useSearchParams();
  const scope: 'sites' | 'rooms' = params.get('scope') === 'rooms' ? 'rooms' : 'sites';
  const siteId = params.get('site');
  const roomId = params.get('room');

  // Facility rooms are hidden by default. A generator hall has no racks, no
  // intake sensors and no capacity to sell, so it contributes a row of dashes
  // that pushes the halls off the first screen. It is a toggle rather than a
  // deletion because those rooms are real and their load is in every total.
  const [includeFacility, setIncludeFacility] = useState(false);
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(25);

  // Ids in the URL, rows resolved against the payload in hand. A row that no
  // longer exists (a stale link) resolves to nothing and the table shows the
  // level above rather than an empty drill.
  const selected = useMemo(
    () => (siteId ? sites.find((s) => s.id === siteId) ?? null : null),
    [sites, siteId],
  );
  const selectedRoom = useMemo(
    () => (roomId && racks ? rooms.find((r) => r.id === roomId) ?? null : null),
    [rooms, roomId, racks],
  );

  const visibleRooms = useMemo(
    () => (includeFacility
      ? rooms
      // Unclassified rooms are shown: null means nobody has classified it, and
      // hiding a room on the strength of a missing field is how a real hall
      // disappears from the estate view.
      : rooms.filter((r) => r.room_class !== 'facility')),
    [rooms, includeFacility],
  );

  const facilityCount = rooms.length - rooms.filter(
    (r) => r.room_class !== 'facility').length;

  const base = useMemo(() => {
    if (selectedRoom && racks) return racks.filter((r) => r.room_id === selectedRoom.id);
    if (selected) return visibleRooms.filter((r) => r.site_id === selected.site_id);
    return scope === 'sites' ? sites : visibleRooms;
  }, [scope, selected, selectedRoom, sites, visibleRooms, racks]);

  const tier: 'sites' | 'rooms' | 'racks' = selectedRoom && racks
    ? 'racks' : (selected || scope === 'rooms') ? 'rooms' : 'sites';

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return base;
    return base.filter((r) =>
      `${r.name} ${r.site_code} ${r.site_name} ${r.floor ?? ''}`.toLowerCase().includes(q));
  }, [base, search]);

  const current = Math.min(page, Math.max(0, Math.ceil(filtered.length / pageSize) - 1));
  const visible = filtered.slice(current * pageSize, (current + 1) * pageSize);

  function update(mutate: (q: URLSearchParams) => void) {
    setParams((prev) => {
      const q = new URLSearchParams(prev);
      mutate(q);
      return q;
    });
    setPage(0);
  }

  function drillInto(row: Row) {
    if (row.kind === 'room' && racks) {
      update((q) => { q.set('room', row.id); });
    } else {
      update((q) => { q.set('site', row.id); q.delete('room'); });
    }
  }

  /** One level up: racks -> that site's rooms (or the rooms scope the
   *  reader came from), rooms -> all sites. */
  function clearDrill() {
    if (selectedRoom) update((q) => { q.delete('room'); });
    else update((q) => { q.delete('site'); q.delete('room'); });
  }

  return {
    scope,
    includeFacility,
    setIncludeFacility: (v: boolean) => { setIncludeFacility(v); setPage(0); },
    facilityCount,
    setScope: (s: 'sites' | 'rooms') => update((q) => {
      if (s === 'rooms') q.set('scope', 'rooms'); else q.delete('scope');
      q.delete('site'); q.delete('room');
    }),
    search, setSearch: (v: string) => { setSearch(v); setPage(0); },
    selected, selectedRoom, tier, drillInto, clearDrill,
    /** Everything matching the filter - what CSV export uses, not just the page. */
    filtered,
    visible,
    page: current, setPage,
    pageSize, setPageSize,
  };
}
