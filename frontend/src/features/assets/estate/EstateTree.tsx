import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type RackSummary, type RoomSummary } from '../../../api/client';

/** Sites and rooms, ranked by what fits.
 *
 *  The operational floor plan answers "what is wrong here". This answers "what
 *  is here and where is there room" - the same endpoints, a different question,
 *  which is why the asset module renders its own rather than borrowing a page
 *  that must not change (docs/22 §1, §6).
 */
export function EstateTree() {
  const { data: rooms, isLoading } = useQuery<{ items: RoomSummary[] }>({
    queryKey: ['rooms'],
    queryFn: api.rooms,
  });

  const { data: racks } = useQuery<{ items: RackSummary[] }>({
    queryKey: ['racks', 'all'],
    queryFn: () => api.racks({ limit: '1000' }),
  });

  if (isLoading) return <p className="muted">Loading…</p>;

  const byRoom = new Map<string, RackSummary[]>();
  for (const rack of racks?.items ?? []) {
    if (!rack.room_id) continue;
    const list = byRoom.get(rack.room_id) ?? [];
    list.push(rack);
    byRoom.set(rack.room_id, list);
  }

  const sites = new Map<string, RoomSummary[]>();
  for (const room of rooms?.items ?? []) {
    const key = room.datacenter_code ?? 'Unassigned';
    sites.set(key, [...(sites.get(key) ?? []), room]);
  }

  return (
    <>
      <h2>Placement</h2>
      {[...sites.entries()].map(([code, siteRooms]) => (
        <section key={code} style={{ marginBottom: 26 }}>
          <h3>{code}</h3>
          <div className="asset-scroll">
            <table>
              <thead>
                <tr>
                  <th>Room</th><th>Racks</th><th>Assets</th>
                  <th>U free</th><th>Rated kW</th>
                </tr>
              </thead>
              <tbody>
                {siteRooms.map((room) => {
                  const roomRacks = byRoom.get(room.id) ?? [];
                  // Racks with no reported free_u are omitted from the sum
                  // rather than counted as zero: "unknown" and "full" are
                  // different answers to "will this fit".
                  const freeU = roomRacks.reduce(
                    (sum, r) => sum + (r.free_u ?? 0), 0);
                  const assets = roomRacks.reduce(
                    (sum, r) => sum + r.device_count, 0);
                  const ratedKw = roomRacks.reduce(
                    (sum, r) => sum + (r.rated_power_kw ?? 0), 0);
                  return (
                    <tr key={room.id}>
                      <td>
                        <Link to={`/assets/estate/rooms/${room.id}`}>{room.name}</Link>
                      </td>
                      <td className="muted">{roomRacks.length}</td>
                      <td className="muted">{assets}</td>
                      <td className="muted">{freeU}U</td>
                      <td className="muted">
                        {ratedKw ? ratedKw.toFixed(1) : '—'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      ))}
    </>
  );
}
