import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type DeviceSummary, type Page } from '../../../api/client';
import { humanise } from '../../../lib/format';

/** Choose devices, by search or by filter.
 *
 *  Shared by the two flows that need a set of machines - a maintenance window's
 *  targets and a contract's covered assets - because "everything of this model
 *  bought in 2024" is one selection and two hundred clicks otherwise.
 *
 *  Selection survives a filter change. That is the whole point: an operator
 *  narrows to one rack, ticks four things, narrows to another, ticks three
 *  more. A picker that cleared on every keystroke would make the filters
 *  useless for exactly the job they exist for.
 */
export function DevicePicker({ selected, onChange, exclude = [] }: {
  selected: string[];
  onChange: (ids: string[]) => void;
  /** Already covered or already targeted - shown as such rather than hidden,
   *  so it is obvious why a device is not offered. */
  exclude?: string[];
}) {
  const [search, setSearch] = useState('');
  const [type, setType] = useState('');
  const [room, setRoom] = useState('');

  const { data: options } = useQuery({
    queryKey: ['asset-filter-options'],
    queryFn: api.assetFilterOptions,
    staleTime: 5 * 60_000,
  });

  const { data: rooms } = useQuery({
    queryKey: ['rooms'],
    queryFn: api.rooms,
    staleTime: 5 * 60_000,
  });

  const { data, isLoading } = useQuery<Page<DeviceSummary>>({
    queryKey: ['picker-devices', search, type, room],
    queryFn: () => api.assetDevices({
      search: search || undefined,
      device_type: type || undefined,
      room_id: room || undefined,
      limit: '200',
    }),
  });

  const rows = data?.items ?? [];
  const excluded = new Set(exclude);
  const chosen = new Set(selected);

  function toggle(id: string) {
    onChange(chosen.has(id)
      ? selected.filter((x) => x !== id)
      : [...selected, id]);
  }

  const selectable = rows.filter((d) => !excluded.has(d.id));
  const allShown = selectable.length > 0 && selectable.every((d) => chosen.has(d.id));

  return (
    <div className="asset-picker">
      <div className="asset-picker-filters">
        <input
          type="search"
          placeholder="Search name, tag, serial"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select value={type} onChange={(e) => setType(e.target.value)}>
          <option value="">All types</option>
          {(options?.device_types ?? []).filter((t) => t.device_count > 0).map((t) => (
            <option key={t.code} value={t.code}>{t.display_name}</option>
          ))}
        </select>
        <select value={room} onChange={(e) => setRoom(e.target.value)}>
          <option value="">All rooms</option>
          {(rooms?.items ?? []).map((r) => (
            <option key={r.id} value={r.id}>
              {r.datacenter_code} · {r.name}
            </option>
          ))}
        </select>
      </div>

      <div className="asset-picker-bar">
        <button
          type="button"
          disabled={selectable.length === 0}
          onClick={() => {
            // Adds or removes only what is CURRENTLY shown, leaving choices
            // made under other filters alone - otherwise "select all" silently
            // discards the work that came before it.
            const shown = selectable.map((d) => d.id);
            onChange(allShown
              ? selected.filter((id) => !shown.includes(id))
              : [...new Set([...selected, ...shown])]);
          }}
        >
          {allShown ? 'Deselect' : 'Select'} these {selectable.length}
        </button>
        <span className="muted">
          {selected.length} selected
          {data?.next_cursor ? ' · more match than are shown — narrow the filters' : ''}
        </span>
        {selected.length > 0 && (
          <button type="button" onClick={() => onChange([])}>Clear selection</button>
        )}
      </div>

      <div className="asset-picker-list">
        {isLoading && <p className="muted">Loading…</p>}
        {!isLoading && rows.length === 0 && (
          <p className="muted">Nothing matches.</p>
        )}
        {rows.map((d) => {
          const isExcluded = excluded.has(d.id);
          return (
            <label
              key={d.id}
              className={`asset-check${isExcluded ? ' is-disabled' : ''}`}
            >
              <input
                type="checkbox"
                checked={chosen.has(d.id)}
                disabled={isExcluded}
                onChange={() => toggle(d.id)}
              />
              <span>{d.name}</span>
              <span className="muted"> · {humanise(d.device_type)}</span>
              <span className="n">
                {isExcluded ? 'already added' : (
                  [d.location.datacenter_code, d.location.rack_name]
                    .filter(Boolean).join(' · ')
                )}
              </span>
            </label>
          );
        })}
      </div>
    </div>
  );
}
