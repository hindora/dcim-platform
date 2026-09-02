import { useMemo, useState } from 'react';
import type { Series } from '../api/client';
import { metricLabel } from '../lib/format';

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
const LINE_COLORS = [
  '#3b82f6', '#db6d28', '#a855f7', '#2ea043', '#22d3ee', '#d29922',
];

/** Where a line must be broken rather than drawn through.
 *
 *  Telemetry has real gaps: a device goes offline, a poll fails, an endpoint is
 *  retired. Joining across one draws a confident straight line through a period
 *  where nothing was measured, which is the single most misleading thing a
 *  chart can do. Anything longer than a few buckets is treated as absence. */
const GAP_FACTOR = 3;

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

function timeLabel(ms: number, spanMs: number): string {
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, '0');
  // A day or less is a clock reading; anything longer needs the date, or every
  // tick on a month-long chart says 00:00.
  if (spanMs <= 36 * 3600_000) return `${p(d.getHours())}:${p(d.getMinutes())}`;
  return `${d.getMonth() + 1}/${p(d.getDate())}`;
}

function stamp(ms: number): string {
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** What a series is called on the legend and in the tooltip.
 *
 *  The instance when there is one - a port name, an outlet - because that is
 *  what distinguishes two lines of the same metric. Otherwise the metric's
 *  display name, so a chart of three different metrics reads as words rather
 *  than as three shades. */
function seriesLabel(s: Series): string {
  return s.instance || metricLabel(s.metric);
}

export function TimeChart({ series, unit, bucketMs }: {
  series: Series[];
  unit: string;
  /** Width of one aggregate bucket, used to decide what counts as a gap. */
  bucketMs: number;
}) {
  /** The time the pointer is nearest, in epoch ms. Null when not hovering. */
  const [at, setAt] = useState<number | null>(null);

  const model = useMemo(() => {
    const pts = series.flatMap((s) => s.points);
    if (!pts.length) return null;
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]).filter((v) => Number.isFinite(v));
    if (!ys.length) return null;

    const x0 = Math.min(...xs);
    const x1 = Math.max(...xs);
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
      return { key: `${s.metric}|${s.instance}`, label: seriesLabel(s), d };
    });

    // Every distinct timestamp, sorted, so the crosshair can snap to a real
    // sample rather than interpolating a reading nobody took.
    const times = [...new Set(xs)].sort((a, b) => a - b);

    return { paths, x0, x1, y0, y1, sx, sy, span: x1 - x0, times };
  }, [series, bucketMs]);

  /** The readings at the hovered time, one per series that has one there. */
  const hover = useMemo(() => {
    if (at === null || !model) return null;
    const rows = series.map((s, i) => {
      // Exact match only. A series with a gap here genuinely has no reading,
      // and showing its neighbour's would invent one.
      const hit = s.points.find((p) => p[0] === at && Number.isFinite(p[1]));
      return hit
        ? { label: seriesLabel(s), value: hit[1], color: LINE_COLORS[i % LINE_COLORS.length] }
        : null;
    }).filter((r): r is { label: string; value: number; color: string } => r !== null);
    return rows.length ? { at, rows } : null;
  }, [at, model, series]);

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
  const tipW = 138;
  const tipH = 16 + (hover?.rows.length ?? 0) * 14;
  const tipX = hover
    ? (model.sx(hover.at) + tipW + 8 > W - PAD_R
        ? model.sx(hover.at) - tipW - 8
        : model.sx(hover.at) + 8)
    : 0;

  return (
    <div className="chart-wrap">
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img"
           aria-label={`${series.map(seriesLabel).join(', ')} over time, in ${unit}`}>
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
        {model.paths.map((p, i) => (
          <path key={p.key} d={p.d} className="chart-line"
                stroke={LINE_COLORS[i % LINE_COLORS.length]} />
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
              {stamp(hover.at)}
            </text>
            {hover.rows.map((r, i) => (
              <g key={r.label}>
                <rect x={tipX + 7} y={PAD_T + 20 + i * 14} width={7} height={7}
                      rx={1.5} fill={r.color} />
                <text className="chart-tip-label" x={tipX + 19} y={PAD_T + 27 + i * 14}>
                  {r.label}
                </text>
                <text className="chart-tip-value" x={tipX + tipW - 7}
                      y={PAD_T + 27 + i * 14} textAnchor="end">
                  {fmt(r.value)}{unit === 'pct' ? '%' : ''}
                </text>
              </g>
            ))}
          </g>
        )}

        {/* Last, so it takes the pointer. Covers the plot area only - the axes
            are not hoverable and should not move the crosshair. */}
        <rect x={PAD_L} y={PAD_T} width={W - PAD_L - PAD_R} height={H - PAD_T - PAD_B}
              fill="transparent" onMouseMove={onMove} onMouseLeave={() => setAt(null)} />
        <text x={4} y={PAD_T + 2} className="chart-unit">{unit}</text>
      </svg>

      {/* Two or more lines are never told apart by colour alone. One line needs
          no legend - the caption above already names it. */}
      {series.length > 1 && (
        <ul className="chart-legend">
          {model.paths.map((p, i) => (
            <li key={p.key}>
              <span className="swatch"
                    style={{ background: LINE_COLORS[i % LINE_COLORS.length] }} />
              {p.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
