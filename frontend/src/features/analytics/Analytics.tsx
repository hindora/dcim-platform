import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type RoomSummary } from '../../api/client';
import { CapacityView } from './CapacityView';
import { CoolingView } from './CoolingView';
import { ForecastView } from './ForecastView';
import { PowerView } from './PowerView';
import { PueView } from './PueView';
import { ThermalView } from './ThermalView';

/** The analytics section.
 *
 *  One scope picker at the top rather than one per panel, because every
 *  question underneath is asked about the same room: is this room hot, is it
 *  full, what does it cost to cool, when does it run out.
 *
 *  Each panel is responsible for rendering its own refusals. That is not a
 *  fallback path here - the backend declines to answer more often than it
 *  answers, by design, and a UI that renders a decline as an empty chart turns
 *  a deliberate silence back into an implied zero.
 */

export type Tab = 'pue' | 'capacity' | 'thermal' | 'cooling' | 'power' | 'forecast';

const TABS: { key: Tab; label: string; blurb: string }[] = [
  { key: 'capacity', label: 'Capacity', blurb: 'Power, cooling, space and ports — and which runs out first' },
  { key: 'forecast', label: 'Forecast', blurb: 'Where the load is heading, or why that cannot be said yet' },
  { key: 'pue', label: 'PUE', blurb: 'Facility energy over IT energy, with the measurement level attached' },
  { key: 'thermal', label: 'Thermal', blurb: 'Rack ΔT, hot spots, and whether a CRAH is failing or just fed hot air' },
  { key: 'cooling', label: 'Cooling plant', blurb: 'Chiller staging, loop ΔT, and whether the readings agree' },
  { key: 'power', label: 'Power', blurb: 'Redundancy census and the loads with one feed' },
];

/** `initialTab` lets the top-level nav land straight on Thermal or Power
 *  without the reader having to find the tab strip. */
export function Analytics({ initialTab = 'capacity' }: { initialTab?: Tab }) {
  const [tab, setTab] = useState<Tab>(initialTab);
  const [roomId, setRoomId] = useState<string>('');

  const { data: rooms } = useQuery<{ items: RoomSummary[] }>({
    queryKey: ['rooms'],
    queryFn: api.rooms,
    staleTime: 300_000,
  });

  const items = rooms?.items ?? [];
  const room = useMemo(
    () => items.find((r) => r.id === roomId) ?? items[0],
    [items, roomId],
  );
  const active = TABS.find((t) => t.key === tab)!;

  return (
    <>
      <h2>Analytics</h2>
      <p className="subtitle">{active.blurb}</p>

      <div className="toolbar">
        <label htmlFor="scope">Room</label>
        <select id="scope" value={room?.id ?? ''}
                onChange={(e) => setRoomId(e.target.value)}>
          {items.map((r) => (
            <option key={r.id} value={r.id}>
              {r.datacenter_code ? `${r.datacenter_code} · ` : ''}{r.name}
            </option>
          ))}
        </select>
        {room?.datacenter_code && (
          <span className="muted">
            plant-wide panels report the whole of {room.datacenter_code}
          </span>
        )}
      </div>

      <nav className="tabs">
        {TABS.map((t) => (
          <button key={t.key} type="button"
                  className={t.key === tab ? 'tab active' : 'tab'}
                  onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </nav>

      {!room ? (
        <p className="muted">No rooms in inventory.</p>
      ) : (
        <div className="panel-body">
          {tab === 'capacity' && <CapacityView room={room} />}
          {tab === 'forecast' && <ForecastView room={room} />}
          {tab === 'pue' && <PueView room={room} />}
          {tab === 'thermal' && <ThermalView room={room} />}
          {tab === 'cooling' && <CoolingView room={room} />}
          {tab === 'power' && <PowerView room={room} />}
        </div>
      )}
    </>
  );
}
