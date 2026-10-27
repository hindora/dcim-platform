/** The power panels in a room, each read through the meter clamped on it.
 *
 *  A hall's power arrives through its two RPPs, one per side, and neither
 *  has a protocol of its own. What the page can show is the Verdigris meter
 *  on each: the panel's supply, its three phase currents and how far out of
 *  balance they are, and every branch circuit with what it feeds. Under the
 *  room's rack table, because this is where "which breaker is that rack on"
 *  and "is there a spare circuit" get answered - the same component the
 *  device page and the floor plan use, so the three never disagree.
 */

import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api } from '../../api/client';
import { PanelMetering, meteringWords } from '../../components/PanelMetering';

export function RoomPanels({ roomId, roomName }: { roomId: string; roomName: string }) {
  const { data, error, isLoading } = useQuery({
    queryKey: ['room-panels', roomId],
    queryFn: () => api.roomPanels(roomId),
    refetchInterval: 60_000,
    placeholderData: (prev) => prev,
  });
  if (isLoading) return <div className="estate-panel"><p className="muted">Loading the panels…</p></div>;
  if (error) return <div className="estate-panel"><p className="muted">Could not load the room's panels.</p></div>;
  if (!data || data.panels.length === 0) return null;
  return (
    <div className="estate-panel pw-panels">
      <div className="estate-selected">
        <span className="who">Power panels in {roomName}</span>
        <span className="muted small">
          {data.panels.length} panel{data.panels.length === 1 ? '' : 's'}, each read through the meter clamped on it
        </span>
      </div>
      <div className="pw-panels-grid">
        {data.panels.map((p) => {
          const words = meteringWords(p.meters);
          return (
            <section key={p.device_id} className="pw-panel">
              <h4>
                <Link to={`/devices/${p.device_id}`}>{p.name}</Link>
                {words && <span className={`muted small${words.lost ? ' critical' : ''}`}> · {words.text}</span>}
              </h4>
              {p.meters.length
                ? <PanelMetering meters={p.meters} variant="page" />
                : <p className="muted">No meter is clamped on this panel.</p>}
            </section>
          );
        })}
      </div>
    </div>
  );
}
