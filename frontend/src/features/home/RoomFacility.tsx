/** The facility-room body of the room drawer.
 *
 *  A plant room, a switchroom and a generator room are not small data halls.
 *  The drawer used to render every room with the white-space layout - ASHRAE
 *  intake compliance, a room PUE, rack-space utilisation - and in these rooms
 *  all three are category errors: 27 C is a normal plant room and a warning on
 *  a server intake, PUE divides by an IT load that is not there, and the racks
 *  here hold controls rather than load.
 *
 *  The worst of it was the power block, which read 0.0 kW in every electrical
 *  room. That is the estate power split working correctly - it excludes the
 *  `power` category so a PDU's throughput is not added to the servers it
 *  already meters - but every machine in a switchroom IS that category, so the
 *  room whose whole purpose is power reported none of it. A confident zero
 *  over a 195 kW incoming feed.
 *
 *  So this renders what the PLANT tab already knows instead. It replaces the
 *  white-space sections rather than sitting beside them.
 */
import type { FacilityMachine, FacilityRoom } from '../../api/client';
import { Tip } from '../../components/HoverTip';

function Tile({ value, unit, caption, note, absent, bar, detail }: {
  value: React.ReactNode; unit?: string; caption: string;
  note?: string | null; absent?: boolean; bar?: string;
  /** Long provenance, behind a hover mark. Nothing is hidden from a reader who
   *  wants it; the tile just stops spending four lines on it. */
  detail?: string | null;
}) {
  return (
    <div className={`kpi-tile ${absent ? 'absent' : ''}`}>
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
}

function num(v: number | null | undefined, digits = 1): React.ReactNode {
  return v === null || v === undefined ? '—' : v.toFixed(digits);
}

/** The readings that matter for one class of machine, in the order somebody
 *  standing at the panel would read them off.
 *
 *  Deliberately NOT every non-null field. A tower's story is its approach to
 *  wet bulb; a UPS's is how long the batteries would last and how healthy they
 *  are; a board's is whether the phases are balanced. Printing all thirty
 *  fields for each would bury every one of those in the other twenty-nine.
 */
export function readings(m: FacilityMachine): { label: string; value: string }[] {
  const n = (v: number | null | undefined, d = 1, u = '') =>
    (v === null || v === undefined ? null : v.toFixed(d) + u);
  const out: [string, string | null][] = [];

  switch (m.device_type) {
    case 'chiller':
      out.push(['COP', n(m.cop, 2)], ['duty', n(m.duty_pct, 0, ' %')],
        ['CHW', n(m.supply_c, 1, ' °C')], ['ΔT', n(m.delta_t_k, 1, ' K')],
        ['compressor', n(m.compressor_pct, 0, ' %')]);
      break;
    case 'cooling_tower':
      // Approach leads: it is the only number here that says whether the cell
      // is doing its job rather than merely turning. Basin level and vibration
      // are the two that go bad before the tower does.
      out.push(['approach', n(m.approach_k, 1, ' K')],
        ['wet bulb', n(m.wet_bulb_c, 1, ' °C')],
        ['fan', n(m.fan_pct, 0, ' %')], ['basin', n(m.basin_pct, 0, ' %')],
        ['vibration', n(m.vibration, 2)]);
      break;
    case 'pump':
      out.push(['speed', n(m.pump_pct, 0, ' %')], ['VFD', n(m.vfd_hz, 1, ' Hz')],
        ['ΔP', n(m.diff_pressure, 1)], ['motor', n(m.motor_temp_c, 1, ' °C')]);
      break;
    case 'valve':
      out.push(['open', n(m.valve_pct, 0, ' %')],
        ['commanded', n(m.commanded_pct, 0, ' %')],
        ['deviation', n(m.deviation_pct, 1, ' %')]);
      break;
    case 'ups':
      // Runtime and state of health, not just load: during an outage the only
      // question is how many minutes are left and whether the string can
      // deliver them.
      out.push(['load', n(m.duty_pct, 0, ' %')],
        ['carries', n(m.carried_kw, 1, ' kW')],
        ['battery', n(m.battery_health_pct, 0, ' % SoH')],
        ['runtime', n(m.battery_minutes, 0, ' min')],
        ['battery temp', n(m.battery_c, 1, ' °C')]);
      break;
    case 'generator':
      out.push(['fuel', n(m.fuel_pct, 0, ' %')], ['load', n(m.duty_pct, 0, ' %')],
        ['coolant', n(m.coolant_c, 1, ' °C')],
        ['run hours', n(m.run_hours, 0)]);
      break;
    case 'ats':
      out.push(['volts', n(m.voltage_v, 0, ' V')],
        ['transfers', n(m.transfers, 0)]);
      break;
    case 'switchgear': case 'mcc': case 'mpp':
      out.push(['carries', n(m.carried_kw, 1, ' kW')],
        ['load', n(m.duty_pct, 0, ' %')],
        ['imbalance', n(m.imbalance_pct, 1, ' %')],
        ['PF', n(m.power_factor, 2)]);
      break;
    case 'utility_feed': case 'energy_monitor':
      out.push(['carries', n(m.carried_kw, 1, ' kW')],
        ['peak', n(m.peak_kw, 0, ' kW')], ['PF', n(m.power_factor, 2)],
        ['THD', n(m.thd_pct, 1, ' %')],
        ['imbalance', n(m.imbalance_pct, 1, ' %')]);
      break;
    case 'sensor':
      out.push(['air', n(m.ambient_c, 1, ' °C')],
        ['supply', n(m.supply_c, 1, ' °C')],
        ['return', n(m.return_c, 1, ' °C')],
        ['flow', n(m.flow_l_s, 1, ' L/s')]);
      break;
    default:
      out.push(['draw', n(m.power_kw, 2, ' kW')]);
  }
  return out
    .filter((pair): pair is [string, string] => pair[1] !== null)
    .map(([label, value]) => ({ label, value }));
}

/** Phase imbalance past ~2 % is where NEMA motor derating starts, which makes
 *  it the one reading on a board that earns a colour of its own. */
export function machineTone(m: FacilityMachine): string | undefined {
  if (m.alarms_open > 0 || m.verdict === 'critical') return 'critical';
  if (m.verdict === 'ok' || m.verdict === 'standby') {
    return m.imbalance_pct != null && m.imbalance_pct > 2 ? 'warn' : undefined;
  }
  return 'warn';
}

export function RoomFacility({ fac }: { fac: FacilityRoom }) {
  const s = fac.summary;
  const byType = Object.entries(s.by_type).sort((a, b) => b[1] - a[1]);

  return (
    <>
      <section className="drawer-section">
        <div className="title">
          EQUIPMENT
          {s.unmonitored > 0 && (
            <span className="why">{s.unmonitored} never wired for monitoring</span>
          )}
        </div>
        <div className="drawer-grid">
          <Tile value={s.equipment} caption="Machines"
                note={byType.map(([t, c]) => `${c} ${t.replace(/_/g, ' ')}`).join(' · ')} />
          <Tile value={s.active} caption="Running"
                note={`${s.machines_stated} of ${s.equipment} publish a run state`} />
          {/* Standby is counted, never judged. A plant with machines staged off
              has spare capacity; it does not have a fault. */}
          <Tile value={s.standby} caption="Standby"
                note="healthy and available to stage on" />
          {/* Most of these carry no alarm rule, so without this tile a UPS on
              battery reads as a perfectly quiet room. */}
          <Tile value={s.attention} caption="Needs attention"
                bar={s.attention > 0 ? 'warn' : 'ok'}
                note={s.attention
                  ? s.attention_names.join(', ')
                  : 'nothing on battery, bypass or a dead bus'} />
        </div>
      </section>

      <section className="drawer-section">
        <div className="title">
          ROOM
          {s.why && <span className="why">{s.why}</span>}
        </div>
        <div className="drawer-grid">
          {/* Named for whatever measured it. A roof has no room air: the towers
              standing on it read the outdoor air, and for that room it is not a
              proxy for the temperature - it is the temperature. */}
          <Tile absent={s.temp_c === null} value={num(s.temp_c)} unit="°C"
                caption="Room temperature" note={s.temp_source} />
          {/* Throughput, read off ONE class of machine. A switchroom meters the
              same kilowatt at the incoming feed, at the board and again at the
              UPS output, and adding those would treble it. */}
          <Tile absent={s.carried_kw === null} value={num(s.carried_kw)} unit="kW"
                caption="Power carried"
                note={s.carried_by
                  ? `metered at the ${s.carried_by.replace(/_/g, ' ')}`
                  : 'nothing here meters throughput'} />
          <Tile absent={s.heat_kw === null} value={num(s.heat_kw)} unit="kW"
                caption="Heat rejected"
                note={s.heat_kw === null
                  ? 'nothing here moves measured heat'
                  : 'by the machines now running'} />
          {/* Consumption, kept apart from throughput on purpose: one is burnt
              here and the other is only passing through. */}
          <Tile absent={s.power_kw === null} value={num(s.power_kw)} unit="kW"
                caption="Own draw"
                note={s.power_kw === null
                  ? 'no machine here burns metered power'
                  : 'consumed here, not passing through'} />
        </div>
      </section>

      <section className="drawer-section">
        <div className="title">WHAT IS IN HERE</div>
        <div className="fac-list">
          {fac.machines.map((m) => {
            const tone = machineTone(m);
            const reads = readings(m);
            return (
              <div key={m.id} className={`fac-row${tone ? ` ${tone}` : ''}`}>
                <div className="fac-head">
                  <span className="fac-name">{m.name}</span>
                  <span className="fac-type">{m.device_type.replace(/_/g, ' ')}</span>
                  <span className="fac-spacer" />
                  {m.alarms_open > 0 && (
                    <span className="fac-alm">{m.alarms_open} open</span>
                  )}
                  {/* "Not monitored" is a different fact from "quiet": one has
                      never been wired, the other has stopped talking. */}
                  <span className={`fac-state${m.monitored ? '' : ' unset'}`}>
                    {m.monitored ? (m.state_label ?? '—') : 'not monitored'}
                  </span>
                </div>
                <div className="fac-reads">
                  {reads.length === 0
                    ? <span className="unset">no analogue points</span>
                    : reads.map((r) => (
                        <span key={r.label}><em>{r.label}</em> {r.value}</span>
                      ))}
                </div>
              </div>
            );
          })}
        </div>
      </section>
    </>
  );
}
