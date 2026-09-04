import { useState } from 'react';
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
/** The rooms placement work actually happens in: the IT halls and the
 *  network room. Everything else - plant rooms, roof, UPS room - holds
 *  floor-standing gear or the odd instrument rack, and opens on demand
 *  rather than padding every site's table. */
const MAIN_ROOMS = new Set(['Network Room', 'Server Hall A', 'Server Hall B']);

/** An ⓘ that shows a small card on hover or keyboard focus. The card is
 *  position: fixed at coordinates read from the icon, because the table
 *  lives in an .asset-scroll container whose overflow clips anything
 *  absolutely positioned taller than the table itself. */
function InfoTip({ label, children }: {
  label: string; children: React.ReactNode;
}) {
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  const show = (e: React.SyntheticEvent<HTMLElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    setPos({ x: r.right + 4, y: r.bottom + 8 });
  };
  return (
    <span
      className="asset-info"
      tabIndex={0}
      aria-label={label}
      onMouseEnter={show}
      onFocus={show}
      onMouseLeave={() => setPos(null)}
      onBlur={() => setPos(null)}
    >
      ⓘ
      {pos && (
        <span className="asset-tip" role="tooltip"
              style={{ top: pos.y, left: Math.max(8, pos.x - 268) }}>
          {children}
        </span>
      )}
    </span>
  );
}

export function EstateTree() {
  const [showOther, setShowOther] = useState(false);

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

  const isMain = (room: RoomSummary) => MAIN_ROOMS.has(room.name);
  const otherCount = [...sites.values()].flat().filter((r) => !isMain(r)).length;

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
        <h2 style={{ marginRight: 'auto' }}>Placement</h2>
        {otherCount > 0 && (
          <button type="button" className="row-btn"
                  onClick={() => setShowOther((v) => !v)}>
            {showOther ? '− Facility' : '+ Facility'}
          </button>
        )}
      </div>
      {[...sites.entries()].map(([code, siteRooms]) => (
        <section key={code} style={{ marginBottom: 26 }}>
          <h3>{code}</h3>
          <div className="asset-scroll">
            <table>
              <thead>
                <tr>
                  <th>Room</th><th>Racks</th><th>Assets</th>
                  <th>U free</th>
                  <th>
                    Rated kW
                    <InfoTip label="About the Rated kW column">
                      <b>Rack power rating</b>
                      <ul>
                        <li>Room value is the sum of its racks’ ratings</li>
                        <li>A rack’s rating is its smallest single rack-PDU
                          nameplate</li>
                        <li>Under 2N a rack must run on one feed — never
                          the sum of both</li>
                        <li>— means the rack has no rack PDUs (plant
                          instrument racks)</li>
                      </ul>
                    </InfoTip>
                  </th>
                </tr>
              </thead>
              <tbody>
                {siteRooms.filter((r) => showOther || isMain(r)).map((room) => {
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
