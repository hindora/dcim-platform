import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import {
  api, type DeviceSummary, type Page, type RackSummary,
} from '../../../api/client';
import { humanise } from '../../../lib/format';
import { rackLabel, rowLabel } from './labels';

/** The racks in one room, walked the way the floor is: row by row.
 *
 *  Each row is its own line under its own heading, because that is how
 *  somebody standing in the hall finds a cabinet - down row 2, third rack -
 *  and a grid that reflows rows into each other loses that map.
 *
 *  Below the racks, the equipment that stands on the FLOOR: CRAHs, power
 *  panels, wall sensors. It is in this room and on this page's question -
 *  "what is here" - but it holds no rack units, so a tile grid of cabinets
 *  cannot carry it.
 */
export function RoomView() {
  const { id = '' } = useParams();

  const { data, isLoading, error } = useQuery<{ items: RackSummary[] }>({
    queryKey: ['racks', 'room', id],
    queryFn: () => api.racks({ room_id: id, limit: '500' }),
    enabled: Boolean(id),
  });

  // Everything in the room; the floor-standing section is the part with no
  // rack. One query rather than a filtered one so the racked count can
  // cross-check the tiles.
  const { data: devices } = useQuery<Page<DeviceSummary>>({
    queryKey: ['asset-devices', 'room', id],
    queryFn: () => api.assetDevices({ room_id: id, limit: '500' }),
    enabled: Boolean(id),
  });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (isLoading) return <p className="muted">Loading…</p>;

  const racks = data?.items ?? [];
  const roomName = racks[0]?.room_name
    ?? devices?.items[0]?.location.room_name ?? 'Room';

  // Row by row, each row's racks in rack order.
  const byRow = new Map<string, RackSummary[]>();
  for (const rack of racks) {
    const key = rack.row_name ?? '—';
    byRow.set(key, [...(byRow.get(key) ?? []), rack]);
  }
  // Numeric collation, or R10 files before R2.
  const rows = [...byRow.entries()]
    .sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true }));

  const floorStanding = (devices?.items ?? [])
    .filter((d) => !d.location.rack_id)
    .sort((a, b) => a.device_type.localeCompare(b.device_type)
      || a.name.localeCompare(b.name));

  return (
    <>
      <p className="asset-table-note">
        <Link to="/assets/estate">← Placement</Link>
      </p>
      <h2>{roomName}</h2>
      {/* A plant room is not a hall missing its racks: with nothing racked,
          the subtitle says what IS here instead of leading with a zero. */}
      <p className="subtitle">
        {[
          racks.length ? `${racks.length} racks` : null,
          floorStanding.length
            ? `${floorStanding.length} floor-standing devices` : null,
        ].filter(Boolean).join(' · ') || 'Nothing placed here yet'}
      </p>

      {rows.map(([rowName, rowRacks]) => (
        <section key={rowName} style={{ marginBottom: 20 }}>
          <h3 className="asset-charts-head">{rowLabel(rowName)}</h3>
          <div className="asset-tiles">
            {rowRacks
              .slice()
              .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }))
              .map((rack) => {
                const used = rack.u_height - (rack.free_u ?? 0);
                const fill = rack.u_height ? (used / rack.u_height) * 100 : 0;
                return (
                  <Link
                    className="asset-tile"
                    to={`/assets/estate/racks/${rack.id}`}
                    key={rack.id}
                  >
                    <div className="k">{rack.name}</div>
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
        </section>
      ))}

      {/* Only when the room is genuinely empty - a UPS room full of
          switchgear is not missing anything. */}
      {racks.length === 0 && floorStanding.length === 0 && (
        <div className="asset-empty">Nothing placed in this room.</div>
      )}

      {floorStanding.length > 0 && (
        <section style={{ marginBottom: 20 }}>
          <h3 className="asset-charts-head">Floor-standing</h3>
          <div className="asset-tiles">
            {floorStanding.map((d) => (
              <Link className="asset-tile" to={`/assets/inventory/${d.id}`}
                    key={d.id}>
                <div className="k">{humanise(d.device_type)}</div>
                <div className="v" style={{ fontSize: '1.02rem' }}>{d.name}</div>
                <div className="sub">
                  {d.model ?? '—'}
                </div>
              </Link>
            ))}
          </div>
        </section>
      )}
    </>
  );
}
