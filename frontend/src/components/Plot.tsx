import { useMemo } from 'react';

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
}) {
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
      return {
        ...s,
        d: good
          .map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${sx(x).toFixed(1)},${sy(y).toFixed(1)}`)
          .join(' '),
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

    return { paths, bandPath, x0, x1, y0, y1, sx, sy };
  }, [series, band, refs, height, width]);

  if (!model) return <p className="muted">{empty}</p>;

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
