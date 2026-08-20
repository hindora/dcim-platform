import { useMemo } from 'react';
import type { Series } from '../api/client';

/** A small SVG line chart.
 *
 *  One chart per unit. Overlaying degrees and watts on a shared axis produces a
 *  picture where neither line means anything, so the caller groups by unit and
 *  renders one of these per group.
 */

const W = 720;
const H = 180;
const PAD_L = 52;
const PAD_R = 12;
const PAD_T = 10;
const PAD_B = 24;

const LINE_COLORS = [
  '#3b82f6', '#2ea043', '#d29922', '#a855f7', '#db6d28', '#22d3ee',
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

export function TimeChart({ series, unit, bucketMs }: {
  series: Series[];
  unit: string;
  /** Width of one aggregate bucket, used to decide what counts as a gap. */
  bucketMs: number;
}) {
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
      return { key: `${s.metric}|${s.instance}`, label: s.instance || s.metric, d };
    });

    return { paths, x0, x1, y0, y1, sx, sy, span: x1 - x0 };
  }, [series, bucketMs]);

  if (!model) return <p className="muted">No data in this window.</p>;

  const yTicks = niceTicks(model.y0, model.y1);
  const xTicks = [model.x0, (model.x0 + model.x1) / 2, model.x1];

  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img"
         aria-label={`${series[0]?.metric} over time`}>
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
      <text x={4} y={PAD_T + 2} className="chart-unit">{unit}</text>
    </svg>
  );
}
