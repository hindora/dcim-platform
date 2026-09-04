import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { api, type RackSummary } from '../../../api/client';
import { rackLabel, rowLabel } from './labels';

/** The racks in one room, ranked by space rather than by health.
 *
 *  Free racks are visually distinct because the first question on this screen
 *  is where there is room, and a full rack and an empty one look identical in a
 *  list sorted by name.
 */
export function RoomView() {
  const { id = '' } = useParams();

  const { data, isLoading, error } = useQuery<{ items: RackSummary[] }>({
    queryKey: ['racks', 'room', id],
    queryFn: () => api.racks({ room_id: id, limit: '500' }),
    enabled: Boolean(id),
  });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (isLoading) return <p className="muted">Loading…</p>;

  const racks = data?.items ?? [];
  const roomName = racks[0]?.room_name ?? 'Room';

  return (
    <>
      <p className="asset-table-note">
        <Link to="/assets/estate">← Placement</Link>
      </p>
      <h2>{roomName}</h2>
      <p className="subtitle">{racks.length} racks</p>

      <div className="asset-tiles">
        {racks.map((rack) => {
          const used = rack.u_height - (rack.free_u ?? 0);
          const fill = rack.u_height ? (used / rack.u_height) * 100 : 0;
          return (
            <Link
              className="asset-tile"
              to={`/assets/estate/racks/${rack.id}`}
              key={rack.id}
            >
              <div className="k">{rowLabel(rack.row_name)}</div>
              <div className="v" style={{ fontSize: '1.1rem' }}>
                {rackLabel(rack.name)}
              </div>
              <div className="asset-bar" style={{ marginTop: 8 }}>
                <span style={{ width: `${fill}%` }} />
              </div>
              <div className="sub">
                {rack.device_count} assets · {rack.free_u ?? '?'}U free of{' '}
                {rack.u_height}U
                {rack.rated_power_kw
                  ? ` · ${rack.rated_power_kw.toFixed(1)} kW rated`
                  : ''}
              </div>
            </Link>
          );
        })}
      </div>

      {racks.length === 0 && (
        <div className="asset-empty">No racks in this room.</div>
      )}
    </>
  );
}
