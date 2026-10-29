import { useMemo, useState } from 'react';
import { DASHES, useChartColors } from './seriesColors';
import type { Series } from '../api/client';
import { metricLabel, unitSymbol } from '../lib/format';

/** A small SVG line chart.
 *
 *  One chart per unit. Overlaying degrees and watts on a shared axis produces a
 *  picture where neither line means anything, so the caller groups by unit and
 *  renders one of these per group. There is deliberately no second y-axis: two
 *  scales on one frame let any two series be made to look correlated by
 *  choosing the ranges, which is the most common way a chart lies.
 */

const W = 720;
const H = 180;
const PAD_L = 52;
const PAD_R = 12;
const PAD_T = 10;
const PAD_B = 24;

/** Categorical hues, assigned in this fixed order and never cycled.
 *
 *  Blue -> orange -> purple, validated against both surfaces this app renders
 *  on: the worst adjacent pair separates by dE 29 under protanopia and 17 under
 *  tritanopia, and every step sits inside the lightness band with enough chroma
 *  not to read as grey.
 *
 *  The previous order put amber second, which fails: amber against the green
 *  below it separates by dE 4.4 for a protanope - two lines a red-green
 *  colourblind reader cannot tell apart at all, on a chart whose whole job is
 *  telling them apart. Normal vision was fine, which is why it survived; the
 *  check that catches it is `validate_palette.js`, not the eye.
 *
 *  Beyond three series identity stops being carryable by hue, so the caller
 *  should facet rather than reach further down this list.
 */
/* The ramp lives in index.css as --series-N and is resolved at render, so a
   chart follows the theme and is checked by scripts/validate_palette.js.
   See components/seriesColors.ts for why hue is not the only channel. */

/** Where a line must be broken rather than drawn through.
 *
 *  Telemetry has real gaps: a device goes offline, a poll fails, an endpoint is
 *  retired. Joining across one draws a confident straight line through a period
 *  where nothing was measured, which is the single most misleading thing a
 *  chart can do. Anything longer than a few buckets is treated as absence. */
const GAP_FACTOR = 3;

/** An alarm episode drawn under the time axis: when it opened, when it
 *  cleared (null while still open), and what it was. */
export type ChartMark = { start: number; end: number | null; severity: string; label: string };

/** How an episode reads in a word. */
function severityWord(s: string): string {
  return s.charAt(0) + s.slice(1).toLowerCase();
}

/** Base SI units a chart may carry in a bigger prefix. */
const PREFIXABLE: Record<string, true> = { W: true, Wh: true, VA: true, var: true };

/** The prefix a chart is drawn in: the one that keeps its biggest reading
 *  under five digits. A chiller's 800 kW cooling capacity read "800000 W" on
 *  the axis and in the tooltip - six digits to count before the number meant
 *  anything. The whole chart moves together, axis, tooltip and unit label, so
 *  two lines on one frame are never in different prefixes. */
function scaleFor(unit: string, series: Series[]): { unit: string; div: number } {
  if (!PREFIXABLE[unit]) return { unit, div: 1 };
  let peak = 0;
  for (const s of series) {
    for (const p of s.points) if (Number.isFinite(p[1])) peak = Math.max(peak, Math.abs(p[1]));
    for (const e of s.envelope ?? []) if (Number.isFinite(e[2])) peak = Math.max(peak, Math.abs(e[2]));
  }
  if (peak >= 1e7) return { unit: `M${unit}`, div: 1e6 };
  if (peak >= 1e4) return { unit: `k${unit}`, div: 1e3 };
  return { unit, div: 1 };
}

function niceTicks(lo: number, hi: number, count = 4): number[] {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return [];
  if (lo === hi) return [lo];
  const raw = (hi - lo) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? mag * 10;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
  return out;
}

function fmt(v: number): string {
  const a = Math.abs(v);
  if (a >= 1000) return v.toFixed(0);
  if (a >= 10) return v.toFixed(1);
  if (a >= 1) return v.toFixed(2);
  return v.toPrecision(2);
}

const shortDate = (d: Date) =>
  d.toLocaleDateString([], { month: 'short', day: 'numeric' });
const clock = (d: Date) =>
  d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

function timeLabel(ms: number, spanMs: number): string {
  const d = new Date(ms);
  // A day or less is a clock reading; anything longer needs the date, or every
  // tick on a month-long chart says 00:00.
  return spanMs <= 36 * 3600_000 ? clock(d) : shortDate(d);
}

/** The hovered instant, at the precision the data actually has.
 *
 *  Both halves were wrong. It always printed HH:MM:SS, so a 30-day chart
 *  answered "which day is this peak" with "14:00:00" - and the axis underneath
 *  had already switched to dates, so the two disagreed about what the reader
 *  was even looking at.
 *
 *  The date appears when the visible points span more than one calendar day,
 *  which is the exact condition under which a bare clock reading is ambiguous -
 *  including a six-hour window that happens to cross midnight.
 *
 *  The seconds are gone. The finest bucket here is a minute, so a trailing
 *  ":00" was never a measurement - it was two digits of borrowed precision on
 *  every reading. Where the bucket IS a day the clock goes too: "Sep 2, 00:00"
 *  claims a midnight reading for a daily average.
 */
function stamp(ms: number, bucketMs: number, spansDays: boolean): string {
  const d = new Date(ms);
  if (bucketMs >= 86400_000) return shortDate(d);
  return spansDays ? `${shortDate(d)}, ${clock(d)}` : clock(d);
}

/** What a series is called on the legend and in the tooltip.
 *
 *  The instance when there is one - a port name, an outlet - because that is
 *  what distinguishes two lines of the same metric. Otherwise the metric's
 *  display name, so a chart of three different metrics reads as words rather
 *  than as three shades.
 *
 *  On a chart that mixes metrics the instance alone is not enough: a tower's
 *  water chart carried supply AND return on COND, and the tooltip read "COND
 *  15.5 °C, COND 20.5 °C". There both are said: "Water Supply Temperature ·
 *  COND". */
type HoverRow = { label: string; value: number; color: string; lo?: number; hi?: number };

function seriesLabel(s: Series, mixed = false): string {
  if (!s.instance) return metricLabel(s.metric);
  return mixed ? `${metricLabel(s.metric)} · ${s.instance}` : s.instance;
}

export function TimeChart({ series: raw, unit: rawUnit, bucketMs, marks: rawMarks, window }: {
  series: Series[];
  unit: string;
  /** Width of one aggregate bucket, used to decide what counts as a gap. */
  bucketMs: number;
  /** Alarm episodes on the device, drawn as bars under the time axis. The
   *  chart and the alarm panel then tell one story: a bar under a flat line
   *  says "something happened here that the average hid", and the band
   *  above it usually shows what. */
  marks?: ChartMark[];
  /** The window asked for, [start, end] in epoch ms. The frame reaches to
   *  whichever episode in it is latest, even past the last bucket: a rollup
   *  lags the clock by up to an hour, and an alarm raised since the last
   *  bucket landed is exactly the one the reader came for. The line stops
   *  where the data stops, and the gap after it is the lag, shown. */
  window?: [number, number];
}) {
  const { series, unit } = useMemo(() => {
    const sc = scaleFor(rawUnit, raw);
    if (sc.div === 1) return { series: raw, unit: rawUnit };
    return {
      unit: sc.unit,
      series: raw.map((s) => ({
        ...s,
        points: s.points.map(([t, v]) => [t, Number.isFinite(v) ? v / sc.div : v]),
        envelope: s.envelope?.map(([t, lo, hi]) => [t, lo / sc.div, hi / sc.div]),
      })),
    };
  }, [raw, rawUnit]);
  /** The time the pointer is nearest, in epoch ms. Null when not hovering. */
  const [at, setAt] = useState<number | null>(null);
  const chart = useChartColors();
  const colorOf = (i: number) => chart.series[i % chart.series.length];
  const dashOf = (i: number) => DASHES[i % DASHES.length];
  const mixed = new Set(series.map((s) => s.metric)).size > 1;

  const model = useMemo(() => {
    const pts = series.flatMap((s) => s.points);
    if (!pts.length) return null;
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]).filter((v) => Number.isFinite(v));
    if (!ys.length) return null;
    // The band's extremes set the frame too, or the spike the band exists to
    // show would be clipped at the top of it.
    for (const s of series) {
      for (const e of s.envelope ?? []) {
        if (Number.isFinite(e[1])) ys.push(e[1]);
        if (Number.isFinite(e[2])) ys.push(e[2]);
      }
    }

    // Episodes clamped to the window, so one that opened two days ago and
    // cleared this morning does not drag a six-hour frame back two days.
    const w0 = window?.[0] ?? -Infinity;
    const w1 = window?.[1] ?? Infinity;
    const markXs = (rawMarks ?? []).flatMap((m) => {
      const a = Math.max(m.start, w0);
      const b = Math.min(m.end ?? m.start, w1);
      return b < a ? [] : [a, b];
    });
    const x0 = Math.min(...xs, ...markXs);
    const x1 = Math.max(...xs, ...markXs);
    let y0 = Math.min(...ys);
    let y1 = Math.max(...ys);
    if (y0 === y1) { y0 -= 1; y1 += 1; }          // a flat line still needs a band
    // A little headroom so the line does not ride the frame.
    const padY = (y1 - y0) * 0.08;
    y0 -= padY; y1 += padY;

    const sx = (v: number) =>
      PAD_L + ((v - x0) / Math.max(1, x1 - x0)) * (W - PAD_L - PAD_R);
    const sy = (v: number) =>
      H - PAD_B - ((v - y0) / (y1 - y0)) * (H - PAD_T - PAD_B);

    const paths = series.map((s) => {
      let d = '';
      let prevX: number | null = null;
      for (const [t, v] of s.points) {
        if (!Number.isFinite(v)) { prevX = null; continue; }
        const broken = prevX !== null && t - prevX > bucketMs * GAP_FACTOR;
        d += `${d === '' || broken ? 'M' : 'L'}${sx(t).toFixed(1)},${sy(v).toFixed(1)} `;
        prevX = t;
      }
      return { key: `${s.metric}|${s.instance}`, label: seriesLabel(s, mixed), d };
    });

    // The min-max band behind an averaged line: one closed shape per unbroken
    // run of buckets, the highs drawn forward and the lows drawn back. Broken
    // where the line is, for the same reason.
    const bands = series.map((s) => {
      const env = s.envelope;
      if (!env?.length) return null;
      let d = '';
      let run: number[][] = [];
      const flush = () => {
        if (run.length >= 2) {
          const up = run.map((e) => `${sx(e[0]).toFixed(1)},${sy(e[2]).toFixed(1)}`);
          const down = [...run].reverse().map((e) => `${sx(e[0]).toFixed(1)},${sy(e[1]).toFixed(1)}`);
          d += `M${up.join(' L')} L${down.join(' L')} Z `;
        }
        run = [];
      };
      let prevX: number | null = null;
      for (const e of env) {
        if (!Number.isFinite(e[1]) || !Number.isFinite(e[2])) { flush(); prevX = null; continue; }
        if (prevX !== null && e[0] - prevX > bucketMs * GAP_FACTOR) flush();
        run.push(e);
        prevX = e[0];
      }
      flush();
      return d || null;
    });

    // Alarm episodes clipped to the window. An episode still open runs to
    // the right edge; one shorter than a pixel still gets two, so a
    // forty-second excursion is a mark and not a rounding error.
    const marks = (rawMarks ?? []).flatMap((m) => {
      const a = Math.max(m.start, x0);
      const b = Math.min(m.end ?? x1, x1);
      if (b < a) return [];
      return [{ ...m, x: sx(a), w: Math.max(2, sx(b) - sx(a)) }];
    });

    // Every distinct timestamp, sorted, so the crosshair can snap to a real
    // sample rather than interpolating a reading nobody took.
    const times = [...new Set(xs)].sort((a, b) => a - b);

    // Whether a bare clock reading would be ambiguous: true the moment the
    // first and last points fall on different calendar days.
    const spansDays =
      new Date(x0).toDateString() !== new Date(x1).toDateString();

    return { paths, bands, marks, x0, x1, y0, y1, sx, sy, span: x1 - x0, times, spansDays };
  }, [series, bucketMs, rawMarks, window]);

  /** The readings at the hovered time, one per series that has one there. */
  const hover = useMemo(() => {
    if (at === null || !model) return null;
    const rows = series.map((s, i): HoverRow | null => {
      // Exact match only. A series with a gap here genuinely has no reading,
      // and showing its neighbour's would invent one.
      const hit = s.points.find((p) => p[0] === at && Number.isFinite(p[1]));
      if (!hit) return null;
      // The bucket's extremes beside its average, where there are any: the
      // number the alarm fired on is the max, not the mean.
      const env = s.envelope?.find((e) => e[0] === at);
      const row: HoverRow = { label: seriesLabel(s, mixed), value: hit[1], color: colorOf(i) };
      if (env) { row.lo = env[1]; row.hi = env[2]; }
      return row;
    }).filter((r): r is HoverRow => r !== null);
    return rows.length ? { at, rows } : null;
  }, [at, model, series, chart]);

  if (!model) return <p className="muted">No data in this window.</p>;

  const yTicks = niceTicks(model.y0, model.y1);
  const xTicks = [model.x0, (model.x0 + model.x1) / 2, model.x1];

  function onMove(e: React.MouseEvent<SVGRectElement>) {
    const box = e.currentTarget.getBoundingClientRect();
    if (!box.width || !model) return;
    // Pointer -> user units. The viewBox maps the full 720 across the rendered
    // width, so the chart stays hoverable at any size without a resize hook.
    const ux = ((e.clientX - box.left) / box.width) * (W - PAD_L - PAD_R) + PAD_L;
    const t = model.x0 + ((ux - PAD_L) / (W - PAD_L - PAD_R)) * (model.x1 - model.x0);
    let best = model.times[0];
    for (const c of model.times) {
      if (Math.abs(c - t) < Math.abs(best - t)) best = c;
    }
    setAt(best);
  }

  // Flip the tooltip to the left of the crosshair when it would overflow the
  // right edge, so the last few samples are still readable.
  //
  // Sized to what it holds. A fixed 138 fitted "Ckt01  4.2 W" and nothing
  // longer: "Water Supply Temperature" ran under its own "15.5 °C". The text is
  // 10 px, mono for the time and values (0.6 em a glyph) and sans for labels
  // (taken at the same 6 px, which errs wide for lower case), so the box is
  // swatch + longest label + gap + longest value. Never wider than the plot; a
  // label that still does not fit is cut with an ellipsis and keeps its full
  // name as the row's title.
  const sym = unitSymbol(unit);
  // "22.3 (21.9–25.8) °C": the average, then the bucket's floor and ceiling
  // where the series has them. The ceiling is the reading the reader came
  // to find.
  const valueText = (r: HoverRow) =>
    `${fmt(r.value)}${r.lo != null && r.hi != null ? ` (${fmt(r.lo)}–${fmt(r.hi)})` : ''}${sym ? ` ${sym}` : ''}`;
  const CH = 6;
  const timeText = hover ? stamp(hover.at, bucketMs, model.spansDays) : '';
  const labelChars = Math.max(0, ...(hover?.rows ?? []).map((r) => r.label.length));
  const valueChars = Math.max(0, ...(hover?.rows ?? []).map((r) => valueText(r).length));
  const tipW = Math.min(W - PAD_L - PAD_R,
    Math.max(138, 19 + labelChars * CH + 14 + valueChars * CH + 7, 14 + timeText.length * CH));
  const fitChars = Math.max(4, Math.floor((tipW - 19 - 14 - valueChars * CH - 7) / CH));
  const cut = (l: string) => (l.length > fitChars ? `${l.slice(0, fitChars - 1)}…` : l);
  const tipH = 16 + (hover?.rows.length ?? 0) * 14;
  const tipX = hover
    ? (model.sx(hover.at) + tipW + 8 > W - PAD_R
        ? model.sx(hover.at) - tipW - 8
        : model.sx(hover.at) + 8)
    : 0;

  const hasBand = model.bands.some(Boolean);
  const markColor = (sev: string) =>
    sev === 'CRITICAL' || sev === 'MAJOR' ? chart.critical
      : sev === 'WARNING' || sev === 'MINOR' ? chart.warn : chart.muted;
  const markTitle = (m: ChartMark) =>
    `${severityWord(m.severity)}: ${m.label}\n${stamp(m.start, 60_000, model.spansDays)} – ${
      m.end == null ? 'still open' : stamp(m.end, 60_000, model.spansDays)}`;

  return (
    <div className="chart-wrap">
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img"
           aria-label={`${series.map((s) => seriesLabel(s, mixed)).join(', ')} over time, in ${unitSymbol(unit) || unit}`
             + (hasBand ? '; the band is each bucket\'s lowest to highest reading' : '')
             + (model.marks.length ? `; ${model.marks.length} alarm episode${model.marks.length === 1 ? '' : 's'} marked under the axis` : '')}>
        {yTicks.map((t) => (
          <g key={t}>
            <line x1={PAD_L} x2={W - PAD_R} y1={model.sy(t)} y2={model.sy(t)}
                  className="chart-grid" />
            <text x={PAD_L - 6} y={model.sy(t)} className="chart-tick-y">{fmt(t)}</text>
          </g>
        ))}
        {xTicks.map((t, i) => (
          <text key={t} x={model.sx(t)} y={H - 8}
                className="chart-tick-x"
                textAnchor={i === 0 ? 'start' : i === xTicks.length - 1 ? 'end' : 'middle'}>
            {timeLabel(t, model.span)}
          </text>
        ))}
        {/* Under the lines, in the line's own colour at low opacity: the
            band belongs to its line and must never read as a fifth series. */}
        {model.bands.map((d, i) => d && (
          <path key={model.paths[i].key} d={d} className="chart-band" fill={colorOf(i)} />
        ))}
        {model.paths.map((p, i) => (
          <path key={p.key} d={p.d} className="chart-line"
                stroke={colorOf(i)} strokeDasharray={dashOf(i)} />
        ))}
        {/* Alarm episodes, as bars in the gutter between the plot and the
            tick labels, in the severity's colour. Hoverable on a taller
            invisible strip than the bar itself. */}
        {model.marks.map((m, i) => (
          <g key={`${m.start}-${i}`} className="chart-mark">
            <title>{markTitle(m)}</title>
            <rect x={m.x} y={H - PAD_B + 1.5} width={m.w} height={4} rx={1}
                  fill={markColor(m.severity)} />
            <rect x={m.x} y={H - PAD_B} width={m.w} height={11} fill="transparent" />
          </g>
        ))}

        {hover && (
          <g pointerEvents="none">
            <line className="chart-crosshair"
                  x1={model.sx(hover.at)} x2={model.sx(hover.at)}
                  y1={PAD_T} y2={H - PAD_B} />
            {hover.rows.map((r) => (
              <circle key={r.label} r={4} fill={r.color}
                      className="chart-dot"
                      cx={model.sx(hover.at)} cy={model.sy(r.value)} />
            ))}
            <rect className="chart-tip" x={tipX} y={PAD_T} width={tipW} height={tipH}
                  rx={3} />
            <text className="chart-tip-time" x={tipX + 7} y={PAD_T + 12}>
              {timeText}
            </text>
            {hover.rows.map((r, i) => (
              <g key={r.label}>
                <rect x={tipX + 7} y={PAD_T + 20 + i * 14} width={7} height={7}
                      rx={1.5} fill={r.color} />
                <text className="chart-tip-label" x={tipX + 19} y={PAD_T + 27 + i * 14}>
                  {cut(r.label)}
                  {cut(r.label) !== r.label && <title>{r.label}</title>}
                </text>
                <text className="chart-tip-value" x={tipX + tipW - 7}
                      y={PAD_T + 27 + i * 14} textAnchor="end">
                  {/* The symbol, not the code, and not just for percentages -
                     a reading of "67.9" beside a line labelled C is a number
                     the reader has to carry the unit for. */}
                  {valueText(r)}
                </text>
              </g>
            ))}
          </g>
        )}

        {/* Last, so it takes the pointer. Covers the plot area only - the axes
            are not hoverable and should not move the crosshair. */}
        <rect x={PAD_L} y={PAD_T} width={W - PAD_L - PAD_R} height={H - PAD_T - PAD_B}
              fill="transparent" onMouseMove={onMove} onMouseLeave={() => setAt(null)} />
        <text x={4} y={PAD_T + 2} className="chart-unit">{unitSymbol(unit)}</text>
      </svg>

      {/* Two or more lines are never told apart by colour alone. One line needs
          no legend - the caption above already names it. */}
      {series.length > 1 && (
        <ul className="chart-legend">
          {model.paths.map((p, i) => (
            <li key={p.key}>
              {/* The swatch wears the line's pattern as well as its colour:
                  a legend that shows only hue cannot be read in the one
                  situation the pattern exists for. */}
              {/* Wide enough for a full cycle where there is a pattern to
                  show: dash-dot runs 20 units, and a clipped cycle reads as a
                  different pattern. */}
              <svg className="swatch-line" width={dashOf(i) ? 22 : 14} height={8} aria-hidden>
                <line x1={0} y1={4} x2={dashOf(i) ? 22 : 14} y2={4} stroke={colorOf(i)}
                      strokeWidth={2} strokeDasharray={dashOf(i)} />
              </svg>
              {p.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
