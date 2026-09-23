/** The room drawer.
 *
 *  Same panel as the site drawer one level down: what is in the room, how warm
 *  it is running, what it draws, and how full it is. Every figure that has no
 *  instrument behind it renders as a dash with the reason attached, because on
 *  this screen a zero and an absence look identical until someone acts on one.
 *
 *  TWO layouts, chosen by what the room is. The tiles below describe WHITE
 *  SPACE - server intakes against ASHRAE, a room PUE, rack U used. A plant
 *  room or a switchroom gets `RoomFacility` instead, which is not a reduced
 *  version of this one but a different set of questions; see that file for
 *  why the white-space tiles were wrong there rather than merely empty.
 */
import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type RoomKpi } from '../../api/client';
import { RoomFacility } from './RoomFacility';
import { ALL_CATEGORIES, AlarmTrend, maxOpen } from './AlarmTrend';

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

/** What the room's envelope figure was graded on, in one line.
 *
 *  A share means nothing without the legs behind it: 100 % on a hall with six
 *  humidity probes is a stronger statement than 100 % on a hall with none, and
 *  the second is not a pass on moisture - it is no moisture leg at all. */
function envelopeNote(env: RoomKpi['environmental'] | undefined): string | null {
  if (!env) return null;
  const e = env.envelope;
  if (!e) return `${env.band.low_c}–${env.band.high_c} °C recommended`;
  const legs = [`${env.band.low_c}–${env.band.high_c} °C`];
  legs.push(e.moisture_pct === null
    ? 'no humidity probe'
    : `moisture from ${e.moisture_probes} probe${e.moisture_probes === 1 ? '' : 's'}`);
  if (e.rate_pct !== null) legs.push(`${e.max_rate_k_per_h ?? 20} K/h`);
  return `ASHRAE ${e.ashrae_class ?? 'A1'}: ${legs.join(' · ')}`;
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
    // Not while a maximized chart is up: that Escape is the modal's.
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !maxOpen()) onClose();
    };
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

        {/* ENTER, not "OPEN FLOOR PLAN": this is the same destination the row's
            ENTER already goes to, and the site drawer's primary action is
            called ENTER too. One link with two names in the same table reads
            as two different places. What ENTER means is "go into this scope" -
            for a site that is its device list, for a room its floor plan. */}
        <Link className="enter primary" to={`/floorplan?room=${roomId}`}
              style={{ display: 'flex', alignItems: 'center', justifyContent: 'center',
                       textDecoration: 'none' }}>
          ENTER
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

            {data.facility ? <RoomFacility fac={data.facility} /> : <>

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
                      // Same two lines as the alarm rules: warn above the
                      // recommended band, critical above the allowable one.
                      bar={!env?.max_c ? 'ok'
                        : env.max_c > (env.band?.allowable_high_c ?? 32) ? 'critical'
                        : env.max_c > (env.band?.high_c ?? 27) ? 'warn' : 'ok'} />
                {/* The whole envelope, as on the thermal page: dry bulb AND
                    moisture AND rate of change. This was dry bulb alone under
                    the caption "Readings in band" - honest about itself, but it
                    meant the drawer and the page answered the same question
                    with two different numbers. The note names what the figure
                    could actually be graded on, because a hall with no
                    humidity probe has no moisture leg and must not read as
                    though it passed one. */}
                <Tile absent={(env?.envelope?.envelope_pct ?? env?.compliance_pct) === null}
                      value={num(env?.envelope?.envelope_pct ?? env?.compliance_pct)}
                      unit="%" caption="Time in band"
                      note={envelopeNote(env)} />
                <Tile absent={env?.rh_avg == null} value={num(env?.rh_avg)} unit="%"
                      caption="Humidity" note={env?.humidity_note}
                      bar={env?.rh_max != null && env.rh_max > (env.band?.rh_high_pct ?? 60)
                        ? 'warn' : undefined} />
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

            </>}

            {/* Whether now is normal for this room: what it has raised
                over time, every domain at once. The tiles above are the
                present; this is the run-up to it. */}
            <section className="drawer-section">
              <div className="title">ALARMS RAISED</div>
              <AlarmTrend categories={ALL_CATEGORIES}
                          scope={{ kind: 'room', id: roomId, label: roomName }} />
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
