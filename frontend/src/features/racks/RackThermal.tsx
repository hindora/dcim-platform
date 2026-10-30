import { Link } from 'react-router-dom';
import type { RackThermalBlock, RackThermalPoint } from '../../api/client';
import { useHoverTip } from '../../components/HoverTip';
import { useChartColors } from '../../components/seriesColors';
import { humanise } from '../../lib/format';

/** The air in one rack, by height.
 *
 *  Every device is a row at its rack unit, top of the rack first, with a mark
 *  where its intake reads and - where it reports one - where its exhaust
 *  reads, joined by a bar: the rise across that device. All rows share one
 *  temperature axis with the room's ASHRAE recommended band shaded on it, so
 *  "which U is breathing warm air" is read straight across. Two series, two
 *  fixed colours, named past the axis; the hot flags are the status colours
 *  and nothing else is. */
const PAD_L = 118;   // U label and the device's leading code
const PAD_R = 70;    // series names past the end
const ROW_H = 18;
const AXIS_H = 26;

function fmt(v: number | null | undefined, unit = ''): string {
  return v == null ? '—' : `${v.toFixed(1)}${unit}`;
}

export function ThermalProfile({ t }: { t: RackThermalBlock }) {
  const colors = useChartColors();
  const { bind, tipEl } = useHoverTip();
  const pts = t.points;
  if (!pts.length) {
    return <p className="dv-empty">Nothing in this rack reports an air temperature.</p>;
  }
  const temps = pts.flatMap((p) => [p.intake_c, p.exhaust_c]).filter((v): v is number => v != null);
  // The axis holds the whole allowable envelope and anything beyond it, so a
  // reading out of envelope is drawn out of the band, not clipped into it.
  const lo = Math.floor(Math.min(t.ashrae.allow_low_c, ...temps) / 5) * 5;
  const hi = Math.ceil(Math.max(t.ashrae.allow_high_c + 3, ...temps) / 5) * 5;
  const W = 640;
  const plotW = W - PAD_L - PAD_R;
  const x = (c: number) => PAD_L + ((c - lo) / (hi - lo)) * plotW;
  // Racked devices by height, then the side-channel probes and strips.
  const racked = pts.filter((p) => p.u_start != null).sort((a, b) => (b.u_start ?? 0) - (a.u_start ?? 0));
  const side = pts.filter((p) => p.u_start == null);
  const rows = [...racked, ...side];
  const H = AXIS_H + rows.length * ROW_H + 24;   // room for the band's name under the last row
  const ticks: number[] = [];
  for (let c = lo; c <= hi; c += 5) ticks.push(c);
  const [intakeC, exhaustC] = [colors.series[0], colors.series[1]];
  const sevC = (g: string | null) => (!g || g === 'in_band' ? null : g === 'out' ? colors.critical : colors.warn);
  return (
    <div className="rk-profile">
      <svg viewBox={`0 0 ${W} ${H}`} role="img"
           aria-label={`Intake and exhaust temperature per device, top of the rack first, ${lo} to ${hi} degrees`}>
        {/* The recommended band, and the allowable ceiling as a thin rule. */}
        <rect x={x(t.ashrae.rec_low_c)} y={AXIS_H - 4} width={x(t.ashrae.rec_high_c) - x(t.ashrae.rec_low_c)}
              height={rows.length * ROW_H + 4} className="rk-band" />
        <line x1={x(t.ashrae.allow_high_c)} x2={x(t.ashrae.allow_high_c)} y1={AXIS_H - 4} y2={H - 20}
              className="rk-limit" />
        {ticks.map((c) => (
          <g key={c}>
            <line x1={x(c)} x2={x(c)} y1={AXIS_H - 4} y2={H - 20} className="rk-grid" />
            <text x={x(c)} y={AXIS_H - 10} textAnchor="middle" className="rk-tick">{c}°</text>
          </g>
        ))}
        <text x={x(t.ashrae.rec_low_c)} y={H - 6} className="rk-bandlabel">
          ASHRAE {t.ashrae.class} recommended · allowable to {t.ashrae.allow_high_c}°
        </text>
        {rows.map((p, i) => {
          const y = AXIS_H + i * ROW_H + ROW_H / 2;
          const flag = sevC(p.grade);
          const label = p.u_start != null
            ? (p.u_height && p.u_height > 1 ? `U${p.u_start}–${p.u_start + p.u_height - 1}` : `U${p.u_start}`)
            : (p.kind === 'probe' ? 'probe' : 'side');
          const tip = <><b>{p.name}</b> · {humanise(p.device_type)}
            {p.intake_c != null && <> · intake {fmt(p.intake_c, ' °C')}</>}
            {p.exhaust_c != null && <> · exhaust {fmt(p.exhaust_c, ' °C')}</>}
            {p.cpu_c != null && <> · CPU {fmt(p.cpu_c, ' °C')}</>}
            {p.rh_pct != null && <> · RH {fmt(p.rh_pct, ' %')}</>}</>;
          return (
            <g key={p.id} className="rk-row" {...bind(tip)}>
              <rect x={0} y={y - ROW_H / 2} width={W} height={ROW_H} fill="transparent" />
              <text x={8} y={y + 4} className="rk-u">{label}</text>
              {/* Every name in a rack repeats the rack's own suffix; the
                  leading code is what tells them apart. The full name is
                  in the hover. */}
              <text x={48} y={y + 4} className="rk-name"><title>{p.name}</title>{shortName(p.name)}</text>
              {p.intake_c != null && p.exhaust_c != null && (
                <line x1={x(p.intake_c)} x2={x(p.exhaust_c)} y1={y} y2={y} className="rk-rise" />
              )}
              {p.intake_c != null && (
                <circle cx={x(p.intake_c)} cy={y} r={4.5} fill={flag ?? intakeC} className="rk-dot" />
              )}
              {p.exhaust_c != null && (
                <circle cx={x(p.exhaust_c)} cy={y} r={4.5} fill={exhaustC} className="rk-dot" />
              )}
              {flag && p.intake_c != null && (
                <text x={x(p.intake_c) - 8} y={y + 4} textAnchor="end" className="rk-flag" fill={flag}>
                  {p.intake_c.toFixed(1)}°
                </text>
              )}
              {p.exhaust_c != null && (
                <text x={x(p.exhaust_c) + 8} y={y + 4} className="rk-val">{p.exhaust_c.toFixed(1)}°</text>
              )}
              {p.exhaust_c == null && p.intake_c != null && !flag && (
                <text x={x(p.intake_c) + 8} y={y + 4} className="rk-val">{p.intake_c.toFixed(1)}°</text>
              )}
            </g>
          );
        })}
        {/* Series names in the right gutter, at the top row's height. */}
        <g className="rk-legend" transform={`translate(${W - PAD_R + 10}, ${AXIS_H + 6})`}>
          <circle cx={0} cy={0} r={4} fill={intakeC} /><text x={8} y={4}>intake</text>
          <circle cx={0} cy={16} r={4} fill={exhaustC} /><text x={8} y={20}>exhaust</text>
        </g>
      </svg>
      {tipEl}
    </div>
  );
}

/** The thermal tab: the profile, the figures it folds to, and the hot rows. */
export function RackThermalTab({ t, rackId }: { t: RackThermalBlock; rackId: string }) {
  const hot = t.points.filter((p) => p.grade && p.grade !== 'in_band' && p.intake_c != null);
  return (
    <>
      <section>
        <h3>Air by height
          <span className="lead">{t.sensors} readings · {t.in_band} inside the recommended band</span>
          <Link className="more" to={`/racks/${rackId}?tab=elevation&overlay=thermal`}>On the elevation →</Link>
        </h3>
        <ThermalProfile t={t} />
      </section>
      <div className="dv-grid">
        <section className="dv-card">
          <h3>Figures</h3>
          <div className="dv-facts">
            <Fact k="Intake, mean" v={fmt(t.intake_mean_c, ' °C')} />
            <Fact k="Intake, hottest" v={fmt(t.intake_max_c, ' °C')} tone={t.hot === 'out' ? 'critical' : t.hot ? 'warn' : undefined} />
            <Fact k="Intake, coolest" v={fmt(t.intake_min_c, ' °C')} />
            <Fact k="Exhaust, mean" v={fmt(t.exhaust_mean_c, ' °C')} />
            <Fact k="Exhaust, hottest" v={fmt(t.exhaust_max_c, ' °C')} />
            <Fact k="Rise across the rack" v={fmt(t.delta_t_k, ' K')} />
            <Fact k="Humidity" v={fmt(t.rh_mean_pct, ' %')} />
            <Fact k="Graded against" v={`ASHRAE ${t.ashrae.class}: ${t.ashrae.rec_low_c}–${t.ashrae.rec_high_c} °C, allowable to ${t.ashrae.allow_high_c} °C`} />
          </div>
        </section>
        <section className="dv-card">
          <h3>Outside the band</h3>
          {hot.length === 0
            ? <p className="dv-empty">Every intake in this rack is inside the recommended band.</p>
            : (
              <ul className="dv-rows">
                {hot.map((p) => (
                  <li key={p.id} className="dv-row">
                    <span className="k"><Link to={`/devices/${p.id}`}>{p.name}</Link></span>
                    <span className={`v ${p.grade === 'out' ? 'critical' : 'warn'}`}>
                      {p.intake_c!.toFixed(1)} °C · {gradeWords(p.grade, p.intake_c!, t.ashrae.rec_high_c)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          <p className="dv-note">
            A hot intake at the top of a rack with cool ones below it is exhaust finding its way
            back round: look for an open U, a missing blanking panel or a gap at the aisle end
            before looking at the cooling unit.
          </p>
        </section>
      </div>
    </>
  );
}

/** "SRV18" from "SRV18-DC1-HB-R2-01": the part before the site code. */
function shortName(name: string): string {
  const m = /^(.*?)-DC\d/.exec(name);
  return m ? m[1] : (name.length > 14 ? `${name.slice(0, 13)}…` : name);
}

function gradeWords(g: string | null, c: number, recHigh: number): string {
  if (g === 'out') return c > recHigh ? 'out of allowable, hot' : 'out of allowable, cold';
  if (g === 'in_band' || !g) return 'in band';
  return c > recHigh ? 'above recommended' : 'below recommended';
}

export function Fact({ k, v, tone }: { k: string; v: string; tone?: 'warn' | 'critical' }) {
  return (
    <div className="dv-fact">
      <div className="k">{k}</div>
      <div className={`v${tone ? ` ${tone}` : ''}`}>{v}</div>
    </div>
  );
}

export type { RackThermalPoint };
