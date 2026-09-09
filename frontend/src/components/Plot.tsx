import { useMemo, useState } from 'react';

/** A small SVG plot: lines, an optional uncertainty band, reference levels.
 *
 *  Separate from TimeChart, which draws telemetry series keyed by metric and
 *  instance and breaks lines across collection gaps. This one draws analytics
 *  output - a projection, a PUE trend - where x is whatever the caller says it
 *  is and the interesting parts are the band and the thresholds.
 *
 *  The band is not decoration. A forecast drawn as a single line reads as a
 *  measurement; drawn with its interval it reads as an estimate, which is what
 *  it is.
 *
 *  Two options a time series needs and a projection does not: `breakGaps`
 *  ends a line at a point whose y is null or NaN and starts again at the next
 *  real one, so a period nobody measured is a hole rather than a bridge; and
 *  `hover` adds the crosshair TimeChart has - snapped to a real x, never
 *  interpolated - with every series' value at that x in a tip.
 */

const PAD_L = 56;
const PAD_R = 14;
const PAD_T = 12;
const PAD_B = 26;

export const PLOT_COLORS = {
  primary: '#3b82f6',
  projection: '#a855f7',
  ok: '#2ea043',
  warn: '#d29922',
  critical: '#f85149',
  muted: '#6e7681',
};

export interface PlotSeries {
  label: string;
  points: [number, number][];
  color?: string;
  /** Drawn dashed - used for the projected half of a forecast. */
  dashed?: boolean;
}

export interface PlotBand {
  label?: string;
  /** x, lower, upper */
  points: [number, number, number][];
  color?: string;
}

export interface PlotRef {
  value: number;
  label: string;
  color?: string;
}

function niceTicks(lo: number, hi: number, count = 4): number[] {
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || lo === hi) return [lo];
  const raw = (hi - lo) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? mag * 10;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
  return out;
}

function fmtNum(v: number): string {
  const a = Math.abs(v);
  if (a >= 1000) return v.toFixed(0);
  if (a >= 10) return v.toFixed(1);
  if (a >= 1) return v.toFixed(2);
  return v.toPrecision(2);
}

export function Plot({
  series, band, refs = [], unit, xFormat = (v: number) => String(v),
  yFormat = fmtNum, height = 200, width = 720, empty = 'No data.',
  breakGaps = false, hover = false, xTip,
}: {
  series: PlotSeries[];
  band?: PlotBand;
  refs?: PlotRef[];
  unit: string;
  xFormat?: (v: number) => string;
  yFormat?: (v: number) => string;
  height?: number;
  width?: number;
  empty?: string;
  /** A non-finite y ends the line; the next finite one starts a new run. */
  breakGaps?: boolean;
  /** Crosshair snapped to the nearest x that has a point, with a tip. */
  hover?: boolean;
  /** How the tip names the x it is on; defaults to xFormat. */
  xTip?: (v: number) => string;
}) {
  const [at, setAt] = useState<number | null>(null);
  const model = useMemo(() => {
    const xs: number[] = [];
    const ys: number[] = [];
    for (const s of series) {
      for (const [x, y] of s.points) {
        if (Number.isFinite(x) && Number.isFinite(y)) { xs.push(x); ys.push(y); }
      }
    }
    for (const [x, lo, hi] of band?.points ?? []) {
      if (Number.isFinite(x)) { xs.push(x); ys.push(lo, hi); }
    }
    if (!xs.length) return null;

    // Reference levels are included in the vertical range on purpose: a
    // capacity line off the top of the frame is a capacity line nobody sees,
    // and the whole point of drawing it is to show how close the data is.
    for (const r of refs) if (Number.isFinite(r.value)) ys.push(r.value);

    const x0 = Math.min(...xs);
    const x1 = Math.max(...xs);
    let y0 = Math.min(...ys);
    let y1 = Math.max(...ys);
    if (y0 === y1) { y0 -= 1; y1 += 1; }
    const pad = (y1 - y0) * 0.08;
    y0 -= pad; y1 += pad;

    const sx = (v: number) =>
      PAD_L + ((v - x0) / Math.max(1e-9, x1 - x0)) * (width - PAD_L - PAD_R);
    const sy = (v: number) =>
      height - PAD_B - ((v - y0) / (y1 - y0)) * (height - PAD_T - PAD_B);

    const paths = series.map((s) => {
      const good = s.points.filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y));
      // With breakGaps a non-finite y is a gap: the run ends there and the
      // next finite point moves the pen rather than drawing to it.
      let d = '';
      let pen = false;
      for (const [x, y] of s.points) {
        if (!Number.isFinite(x)) continue;
        if (!Number.isFinite(y)) { if (breakGaps) pen = false; continue; }
        d += `${pen ? 'L' : 'M'}${sx(x).toFixed(1)},${sy(y).toFixed(1)} `;
        pen = true;
      }
      return {
        ...s,
        d: d.trim(),
        // A path with one point draws nothing: "M x,y" with no line after it is
        // an invisible chart that looks like missing data rather than a single
        // reading. One usable bucket out of twelve is exactly the case here.
        dot: good.length === 1
          ? { x: sx(good[0][0]), y: sy(good[0][1]) }
          : null,
      };
    });

    let bandPath = '';
    if (band?.points.length) {
      const up = band.points.map(([x, , hi]) => `${sx(x).toFixed(1)},${sy(hi).toFixed(1)}`);
      const down = [...band.points].reverse()
        .map(([x, lo]) => `${sx(x).toFixed(1)},${sy(lo).toFixed(1)}`);
      bandPath = `M${up.join(' L')} L${down.join(' L')} Z`;
    }

    // Every x any series has a real value at, for the crosshair to snap to.
    const xAll = Array.from(new Set(
      series.flatMap((s) => s.points
        .filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y))
        .map(([x]) => x)))).sort((a, b) => a - b);

    return { paths, bandPath, x0, x1, y0, y1, sx, sy, xAll };
  }, [series, band, refs, height, width, breakGaps]);

  if (!model) return <p className="muted">{empty}</p>;

  function onMove(e: React.MouseEvent<SVGRectElement>) {
    if (!model || !model.xAll.length) return;
    const box = e.currentTarget.getBoundingClientRect();
    const frac = (e.clientX - box.left) / Math.max(1, box.width);
    const t = model.x0 + frac * (model.x1 - model.x0);
    let best = model.xAll[0];
    for (const c of model.xAll) if (Math.abs(c - t) < Math.abs(best - t)) best = c;
    setAt(best);
  }
  const hoverRows = hover && at !== null
    ? series.flatMap((s) => {
        const p = s.points.find(([x, y]) => x === at && Number.isFinite(y));
        return p ? [{ label: s.label, value: p[1], color: s.color ?? PLOT_COLORS.primary }] : [];
      })
    : [];
  const tipW = 150;
  const tipH = 16 + hoverRows.length * 14;
  const tipX = at !== null
    ? (model.sx(at) + tipW + 8 > width - PAD_R ? model.sx(at) - tipW - 8 : model.sx(at) + 8)
    : 0;

  const yTicks = niceTicks(model.y0, model.y1);
  // Three ticks across a zero-width span print the same label three times,
  // which reads as an axis and carries none of the information of one.
  const xTicks = model.x0 === model.x1
    ? [model.x0]
    : [model.x0, (model.x0 + model.x1) / 2, model.x1];

  return (
    <>
      <svg className="chart" viewBox={`0 0 ${width} ${height}`} role="img"
           aria-label={`${series.map((s) => s.label).join(', ')} in ${unit}`}>
        {yTicks.map((t) => (
          <g key={`y${t}`}>
            <line x1={PAD_L} x2={width - PAD_R} y1={model.sy(t)} y2={model.sy(t)}
                  className="chart-grid" />
            <text x={PAD_L - 6} y={model.sy(t)} className="chart-tick-y">{yFormat(t)}</text>
          </g>
        ))}
        {xTicks.map((t, i) => (
          <text key={`x${t}-${i}`} x={model.sx(t)} y={height - 8} className="chart-tick-x"
                textAnchor={i === 0 ? 'start' : i === xTicks.length - 1 ? 'end' : 'middle'}>
            {xFormat(t)}
          </text>
        ))}

        {model.bandPath && (
          <path d={model.bandPath} fill={band?.color ?? PLOT_COLORS.projection}
                fillOpacity={0.16} stroke="none" />
        )}

        {refs.map((r) => (
          <g key={r.label}>
            <line x1={PAD_L} x2={width - PAD_R} y1={model.sy(r.value)} y2={model.sy(r.value)}
                  stroke={r.color ?? PLOT_COLORS.critical} strokeWidth={1}
                  strokeDasharray="5 4" />
            <text x={width - PAD_R} y={model.sy(r.value) - 4} className="chart-tick-x"
                  textAnchor="end" fill={r.color ?? PLOT_COLORS.critical}>
              {r.label}
            </text>
          </g>
        ))}

        {model.paths.map((p) => (
          <g key={p.label}>
            <path d={p.d} className="chart-line"
                  stroke={p.color ?? PLOT_COLORS.primary}
                  strokeDasharray={p.dashed ? '6 4' : undefined} />
            {p.dot && (
              <circle cx={p.dot.x} cy={p.dot.y} r={3}
                      fill={p.color ?? PLOT_COLORS.primary} />
            )}
          </g>
        ))}

        {hover && at !== null && hoverRows.length > 0 && (
          <g pointerEvents="none">
            <line className="chart-crosshair"
                  x1={model.sx(at)} x2={model.sx(at)} y1={PAD_T} y2={height - PAD_B} />
            {hoverRows.map((r) => (
              <circle key={r.label} r={4} fill={r.color} className="chart-dot"
                      cx={model.sx(at)} cy={model.sy(r.value)} />
            ))}
            <rect className="chart-tip" x={tipX} y={PAD_T} width={tipW} height={tipH} rx={3} />
            <text className="chart-tip-time" x={tipX + 7} y={PAD_T + 12}>
              {(xTip ?? xFormat)(at)}
            </text>
            {hoverRows.map((r, i) => (
              <g key={r.label}>
                <rect x={tipX + 7} y={PAD_T + 20 + i * 14} width={7} height={7} rx={1.5}
                      fill={r.color} />
                <text className="chart-tip-label" x={tipX + 19} y={PAD_T + 27 + i * 14}>
                  {r.label}
                </text>
                <text className="chart-tip-value" x={tipX + tipW - 7} y={PAD_T + 27 + i * 14}
                      textAnchor="end">
                  {yFormat(r.value)} {unit}
                </text>
              </g>
            ))}
          </g>
        )}
        {hover && (
          // Last, so it takes the pointer; the plot area only, so the axes
          // do not move the crosshair.
          <rect x={PAD_L} y={PAD_T} width={width - PAD_L - PAD_R}
                height={height - PAD_T - PAD_B} fill="transparent"
                onMouseMove={onMove} onMouseLeave={() => setAt(null)} />
        )}
        <text x={4} y={PAD_T + 2} className="chart-unit">{unit}</text>
      </svg>
      {series.length > 1 && (
        <div className="legend">
          {series.map((s) => (
            <span key={s.label}>
              <i style={{ background: s.color ?? PLOT_COLORS.primary }} />
              {s.label}
            </span>
          ))}
          {band?.label && (
            <span>
              <i style={{ background: band.color ?? PLOT_COLORS.projection, opacity: 0.35 }} />
              {band.label}
            </span>
          )}
        </div>
      )}
    </>
  );
}
