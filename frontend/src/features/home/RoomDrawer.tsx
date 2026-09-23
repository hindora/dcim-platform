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
import { Tip } from '../../components/HoverTip';
import { api, type RoomKpi } from '../../api/client';
import { RoomFacility } from './RoomFacility';
import { PowerSplit } from './PowerSplit';
import { ALL_CATEGORIES, AlarmTrend, maxOpen } from './AlarmTrend';

function Tile({ value, unit, caption, note, absent, bar, detail, to }: {
  value: React.ReactNode; unit?: string; caption: string;
  note?: string | null; absent?: boolean; bar?: string;
  /** Long provenance, behind a hover mark. Nothing is hidden from a reader who
   *  wants it; the tile just stops spending four lines on it. */
  detail?: string | null;
  /** Where the subset this figure counts can be seen. A tile that says
   *  "20/22 power units online" and cannot show you the two is a dead end:
   *  the reader has to close the drawer and rebuild the question somewhere
   *  else. Only set this where the destination really is THIS tile's
   *  population - a link that lands on something broader is worse than none. */
  to?: string;
}) {
  const body = (
    <div className={`kpi-tile ${absent ? 'absent' : ''}${to ? ' linked' : ''}`}>
      {bar && <span className="bar" style={{ background: `var(--${bar})` }} />}
      <div>
        <div className="v">
          <span className="n">{absent ? '—' : value}</span>
          {unit && !absent && <span className="u">{unit}</span>}
        </div>
        <div className="cap">
          {caption}
          {detail && (
            <Tip tip={detail} className="tile-why">
              <span aria-label="How this is measured">?</span>
            </Tip>
          )}
        </div>
        {note && <div className="note">{note}</div>}
      </div>
    </div>
  );
  return to ? <Link className="tile-link" to={to}>{body}</Link> : body;
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

        {/* Same compact strip as the site drawer: the reader came from this
            room's row and does not need it restated in three stacked cards.
            The last-reading age travels here because it qualifies everything
            below it - a panel of figures from nine minutes ago is a different
            claim from the same panel now. */}
        <header className="drawer-top">
          <div className="drawer-top-main">
            <h2>{roomName}</h2>
            {data && (
              <>
                <span className="loc">{data.room.site_code}</span>
                {data.room.floor && (
                  <><span className="sep">·</span>
                    <span className="loc">floor {data.room.floor}</span></>
                )}
                {data.room.room_type && (
                  <><span className="sep">·</span>
                    <span className="loc">{data.room.room_type.replace(/_/g, ' ')}</span></>
                )}
              </>
            )}
          </div>
          <div className="drawer-top-meta">
            {data ? `last reading ${ago(data.last_sample)}` : 'loading…'}
          </div>
          {/* ENTER, not "OPEN FLOOR PLAN": the room row's ENTER already goes to
              this exact URL, and one destination under two names in one table
              reads as two places. */}
          <Link className="drawer-top-enter" to={`/floorplan?room=${roomId}`}>
            ENTER
          </Link>
        </header>

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
              {/* Each of these counts a population, so each one opens it. The
                  offline counts go straight to the offline machines rather
                  than the whole room: "2 offline" is only useful as a
                  question, and the answer was four clicks and a rebuilt
                  filter away on another page. */}
              <div className="drawer-grid">
                <Tile value={mon?.devices ?? 0} caption="Devices"
                      note={mon?.offline ? `${mon.offline} offline` : 'all reporting'}
                      to={mon?.offline
                        ? `/devices?room=${roomId}&status=OFFLINE`
                        : `/devices?room=${roomId}`} />
                <Tile value={mon?.racks ?? 0} caption="Racks"
                      to={`/floorplan?room=${roomId}`} />
                <Tile value={`${mon?.cooling_online ?? 0}/${mon?.cooling_units ?? 0}`}
                      caption="Cooling units online"
                      bar={mon && mon.cooling_units && mon.cooling_online < mon.cooling_units
                        ? 'warn' : 'ok'}
                      to={`/devices?room=${roomId}&category=cooling`} />
                <Tile value={`${mon?.power_online ?? 0}/${mon?.power_units ?? 0}`}
                      caption="Power units online"
                      bar={mon && mon.power_units && mon.power_online < mon.power_units
                        ? 'warn' : 'ok'}
                      to={`/devices?room=${roomId}&category=power`} />
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
                {/* The rack behind the number. Without this the reader has to
                    leave, find the thermal page and rebuild the same drill. */}
                <Tile absent={env?.max_c === null} value={num(env?.max_c)} unit="°C"
                      caption="Hottest intake"
                      to={data ? `/thermal?scope=rooms&site=${data.room.datacenter_id}`
                                 + `&room=${roomId}` : undefined}
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
              <PowerSplit
                caption="Room total"
                total={pw?.total_kw}
                segments={[
                  { key: 'it', label: 'IT (AC)', kw: pw?.it_ac_kw, tone: 'accent' },
                  { key: 'cooling', label: 'Cooling', kw: pw?.cooling_kw,
                    tone: 'cool',
                    absentNote: 'no air handler in this room' },
                  { key: 'other', label: 'Other', kw: pw?.other_kw, tone: 'warn',
                    absentNote: 'nothing metered outside IT and cooling' },
                ]} />
              <div className="drawer-grid" style={{ marginTop: 14 }}>
                {/* "In-room", never "Room PUE". This boundary holds only the
                    air handlers standing in the room; the chillers and towers
                    that actually reject the heat are in the plant rooms. A
                    hall reads ~1.09 here while its site reads ~1.31 on the
                    panel one click away, and a reader who compares those
                    without being told is being misled by us. */}
                <Tile absent={pw?.pue === null} value={num(pw?.pue, 3)}
                      caption="In-room PUE"
                      note={pw?.pue === null
                        ? 'no IT load here to divide by'
                        : 'this room only · not the site PUE'}
                      detail={pw?.pue === null ? null : pw?.pue_note} />
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
