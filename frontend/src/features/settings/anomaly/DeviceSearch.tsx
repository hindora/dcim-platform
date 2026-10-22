import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../../api/client';
import { humanise } from '../../../lib/format';

export interface PickedDevice { id: string; name: string; device_type: string }

/** Choose ONE device by name.
 *
 *  `features/assets/components/DevicePicker` is a multi-select (a maintenance
 *  window's targets), and an override or an exclusion is about exactly one
 *  device, so this is the same search as radios. The type filter narrows to
 *  what the caller can use (an override needs a type some detector covers). */
export function DeviceSearch({ value, onChange, types, id }: {
  value: PickedDevice | null;
  onChange: (d: PickedDevice | null) => void;
  /** Only these device types, when given. */
  types?: string[];
  id: string;
}) {
  const [search, setSearch] = useState('');
  const [debounced, setDebounced] = useState('');
  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(search.trim()), 250);
    return () => window.clearTimeout(t);
  }, [search]);

  const q = useQuery({
    queryKey: ['anomaly-device-search', debounced, (types ?? []).join(',')],
    queryFn: () => api.assetDevices({
      search: debounced || undefined,
      device_type: types && types.length ? types : undefined,
      limit: '50',
    }),
    staleTime: 60_000,
  });
  const rows = q.data?.items ?? [];

  return (
    <div className="anom-pick">
      <input id={id} type="search" value={search} placeholder="Search devices by name"
             aria-label="Search devices" onChange={(e) => setSearch(e.target.value)} />
      <div className="anom-pick-list" role="radiogroup" aria-label="Device">
        {q.isLoading && <p className="muted">Loading…</p>}
        {!q.isLoading && rows.length === 0 && <p className="muted">No device matches that.</p>}
        {rows.map((d) => (
          <label key={d.id}>
            <input type="radio" name={`${id}-device`} checked={value?.id === d.id}
                   onChange={() => onChange({ id: d.id, name: d.name, device_type: d.device_type })} />
            <span>{d.name}</span>
            <span className="n">
              {humanise(d.device_type)}
              {d.location?.datacenter_code ? ` · ${d.location.datacenter_code}` : ''}
            </span>
          </label>
        ))}
      </div>
      {value && <span className="muted small">Chosen: {value.name}</span>}
    </div>
  );
}
