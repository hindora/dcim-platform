/** The room drawer.
 *
 *  Same panel as the site drawer one level down: what is in the room, how warm
 *  it is running, what it draws, and how full it is. Every figure that has no
 *  instrument behind it renders as a dash with the reason attached, because on
 *  this screen a zero and an absence look identical until someone acts on one.
 */
import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type RoomKpi } from '../../api/client';

function Tile({ value, unit, caption, note, absent, bar }: {
  value: React.ReactNode; unit?: string; caption: string;
  note?: string | null; absent?: boolean; bar?: string;
}) {
  return (
    <div className={`kpi-tile ${absent ? 'absent' : ''}`}>
      {bar && <span className="bar" style={{ background: `var(--${bar})` }} />}
      <div>
        <div className="v">
          <span className="n">{absent ? '—' : value}</span>
          {unit && !absent && <span className="u">{unit}</span>}
        </div>
        <div className="cap">{caption}</div>
        {note && <div className="note">{note}</div>}
      </div>
    </div>
  );
}

function num(v: number | null | undefined, digits = 1): React.ReactNode {
  return v === null || v === undefined ? '—' : v.toFixed(digits);
}

function pctBar(pct: number | null | undefined): string {
  if (pct === null || pct === undefined) return 'unknown';
  return pct >= 85 ? 'critical' : pct >= 70 ? 'warn' : 'ok';
}

function ago(iso: string | null): string {
  if (!iso) return 'no telemetry in the last 24 hours';
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return 'seconds ago';
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  return `${hours} h ago`;
}

export function RoomDrawer({ roomId, roomName, onClose }: {
  roomId: string; roomName: string; onClose: () => void;
}) {
  const { data, isLoading, error } = useQuery<RoomKpi>({
    queryKey: ['room-kpi', roomId],
    queryFn: () => api.roomKpi(roomId),
    refetchInterval: 30_000,
  });

  // Escape closes. A drawer that can only be dismissed with the mouse is a
  // drawer that traps keyboard users behind it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const env = data?.environmental;
  const pw = data?.power;
  const ut = data?.utilisation;
  const mon = data?.monitored;

  return (
    <>
      <button className="drawer-scrim" onClick={onClose} aria-label="Close room detail" />
      <aside className="drawer" aria-label={`${roomName} detail`}>
        <button className="close" onClick={onClose} aria-label="Close">×</button>

        <div className="drawer-id">
          <div style={{ flex: 1 }}>
            <div className="cap">ROOM</div>
            <div className="val">{roomName}</div>
            <div className="sub">
              {data ? `${data.room.site_code}${data.room.floor ? ` · floor ${data.room.floor}` : ''}`
                    : 'loading…'}
            </div>
          </div>
          <div style={{ flex: 1 }}>
            <div className="cap">LAST READING</div>
            <div className="val">{data ? ago(data.last_sample) : '—'}</div>
            <div className="sub">
              {data?.room.room_type ? data.room.room_type.replace(/_/g, ' ') : ''}
            </div>
          </div>
        </div>

        <Link className="enter primary" to={`/floorplan?room=${roomId}`}
              style={{ display: 'flex', alignItems: 'center', justifyContent: 'center',
                       textDecoration: 'none' }}>
          OPEN FLOOR PLAN
        </Link>

        {error && <div className="banner" style={{ margin: '0 26px 16px' }}>
          Could not load this room.
        </div>}
        {isLoading && <p className="muted" style={{ padding: '0 26px' }}>Loading…</p>}

        {data && (
          <>
            <div className="drawer-head">
              <h3>Live data</h3>
              <span className="as-of">
                as of {new Date(data.as_of).toLocaleTimeString()}
              </span>
            </div>

            <section className="drawer-section">
              <div className="title">MONITORED</div>
              <div className="drawer-grid">
                <Tile value={mon?.devices ?? 0} caption="Devices"
                      note={mon?.offline ? `${mon.offline} offline` : 'all reporting'} />
                <Tile value={mon?.racks ?? 0} caption="Racks" />
                <Tile value={`${mon?.cooling_online ?? 0}/${mon?.cooling_units ?? 0}`}
                      caption="Cooling units online"
                      bar={mon && mon.cooling_units && mon.cooling_online < mon.cooling_units
                        ? 'warn' : 'ok'} />
                <Tile value={`${mon?.power_online ?? 0}/${mon?.power_units ?? 0}`}
                      caption="Power units online"
                      bar={mon && mon.power_units && mon.power_online < mon.power_units
                        ? 'warn' : 'ok'} />
              </div>
            </section>

            <section className="drawer-section">
              <div className="title">
                ENVIRONMENTAL
                {env?.note && <span className="why">{env.note}</span>}
              </div>
              <div className="drawer-grid">
                <Tile absent={env?.avg_c === null} value={num(env?.avg_c)} unit="°C"
                      caption="Average intake" />
                <Tile absent={env?.max_c === null} value={num(env?.max_c)} unit="°C"
                      caption="Hottest intake"
                      bar={env?.max_c && env.max_c > (env.band?.high_c ?? 27) ? 'critical' : 'ok'} />
                <Tile absent={env?.compliance_pct === null} value={num(env?.compliance_pct)}
                      unit="%" caption="Readings in band"
                      note={env ? `${env.band.low_c}–${env.band.high_c} °C recommended` : null} />
                <Tile absent value="—" caption="Humidity" note={env?.humidity_note} />
              </div>
            </section>

            <section className="drawer-section">
              <div className="title">
                POWER
                {pw?.note && <span className="why">{pw.note}</span>}
              </div>
              <div className="drawer-grid">
                <Tile absent={pw?.total_kw === null} value={num(pw?.total_kw)} unit="kW"
                      caption="Room total" />
                <Tile absent={pw?.it_ac_kw === null} value={num(pw?.it_ac_kw)} unit="kW"
                      caption="IT (AC)" />
                <Tile absent={pw?.cooling_kw === null} value={num(pw?.cooling_kw)} unit="kW"
                      caption="Cooling" />
                <Tile absent={pw?.pue === null} value={num(pw?.pue, 3)} caption="Room PUE"
                      note={pw?.pue === null ? 'no IT load here to divide by' : null} />
              </div>
            </section>

            <section className="drawer-section">
              <div className="title">UTILISATION</div>
              <div className="drawer-grid three">
                <Tile absent={ut?.space_pct === null} value={num(ut?.space_pct, 0)} unit="%"
                      caption="Space" bar={pctBar(ut?.space_pct)}
                      note="rack U occupied against rack U installed" />
                <Tile absent={ut?.power_pct === null} value={num(ut?.power_pct, 0)} unit="%"
                      caption="Power" bar={pctBar(ut?.power_pct)} note={ut?.power_basis} />
                <Tile absent={ut?.cooling_pct === null} value={num(ut?.cooling_pct, 0)} unit="%"
                      caption="Cooling" bar={pctBar(ut?.cooling_pct)} note={ut?.cooling_basis} />
              </div>
            </section>

            <div className="drawer-conn">
              <Link to={`/devices?room=${roomId}`}>All devices in this room →</Link>
            </div>
          </>
        )}
      </aside>
    </>
  );
}
