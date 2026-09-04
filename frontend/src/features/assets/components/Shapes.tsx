/** The non-bar chart forms: donut, gauge, vertical columns.
 *
 *  Each exists for one shape of question and is used only there. A donut for a
 *  single part-to-whole, where the centre carries the figure and the ring is
 *  its shape. A gauge for a bounded ratio with thresholds, where low is bad
 *  and the bands say how bad. Vertical columns for a short-labelled SEQUENCE,
 *  where left-to-right is the information.
 *
 *  Long-labelled categoricals stay with the horizontal BarChart - a column
 *  chart forces "OOB Management Switch" to rotate or truncate, and nothing is
 *  bought with that cost.
 */

import { useHoverTip } from '../../../components/HoverTip';

export type Segment = { label: string; n: number; colour: string };

/** A donut: one ratio, its total in the middle, values in the legend.
 *
 *  The centre figure is the point of the form - the number somebody reads
 *  first - and every segment's own value is printed in the legend, because arc
 *  length is the weakest encoding on the page and nobody should have to read
 *  from it. */
export function Donut({ parts, centre, centreLabel }: {
  parts: Segment[];
  centre: string;
  centreLabel: string;
}) {
  const size = 148;
  const thickness = 20;
  const r = (size - thickness) / 2;
  const c = 2 * Math.PI * r;
  const total = Math.max(1, parts.reduce((t, p) => t + p.n, 0));

  let offset = 0;
  const segments = parts.filter((p) => p.n > 0).map((p) => {
    const frac = p.n / total;
    const seg = { ...p, dash: frac * c, offset };
    offset += frac * c;
    return seg;
  });

  return (
    <div className="asset-donut">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}
           role="img" aria-label={`${centreLabel}: ${centre}`}>
        {/* Rotated so the first segment starts at 12 o'clock, which is where a
            ring is read from. */}
        <g transform={`rotate(-90 ${size / 2} ${size / 2})`}>
          {segments.map((s) => (
            <circle
              key={s.label}
              cx={size / 2} cy={size / 2} r={r}
              fill="none"
              stroke={s.colour}
              strokeWidth={thickness}
              strokeDasharray={`${s.dash} ${c - s.dash}`}
              strokeDashoffset={-s.offset}
            />
          ))}
        </g>
        <text x="50%" y="47%" textAnchor="middle" className="asset-donut-centre">
          {centre}
        </text>
        <text x="50%" y="60%" textAnchor="middle" className="asset-donut-sub">
          {centreLabel}
        </text>
      </svg>
      <div className="asset-legend">
        {parts.map((p) => (
          <span key={p.label}>
            <span className="sw" style={{ background: p.colour }} />
            {p.label} {p.n.toLocaleString()}
            <span className="muted"> ({Math.round((p.n / total) * 100)}%)</span>
          </span>
        ))}
      </div>
    </div>
  );
}

/** A semicircular gauge: one bounded value against thresholds.
 *
 *  Bands are fractions of the range, low to high, each with the colour of what
 *  being there MEANS - which is the one thing a gauge does that a bar does
 *  not. The exact figure is printed underneath; the needle is for the glance,
 *  the number is for the answer. */
export function Gauge({ value, max, bands, unit, label }: {
  value: number;
  max: number;
  /** [upTo (0..1), colour] in ascending order; the last should reach 1. */
  bands: [number, string][];
  unit: string;
  label: string;
}) {
  const w = 190;
  const h = 108;
  const cx = w / 2;
  const cy = h - 6;
  const r = 78;
  const thickness = 16;
  const frac = Math.max(0, Math.min(1, value / Math.max(1, max)));

  const point = (f: number, radius: number) => {
    const a = Math.PI * (1 - f);          // 0 -> left, 1 -> right
    return [cx + radius * Math.cos(a), cy - radius * Math.sin(a)] as const;
  };
  const arc = (from: number, to: number) => {
    const [x1, y1] = point(from, r);
    const [x2, y2] = point(to, r);
    return `M ${x1} ${y1} A ${r} ${r} 0 0 1 ${x2} ${y2}`;
  };

  let start = 0;
  const segs = bands.map(([upTo, colour]) => {
    const d = arc(start, upTo);
    start = upTo;
    return { d, colour, key: upTo };
  });

  const [nx, ny] = point(frac, r - thickness / 2 - 2);

  // Quarter ticks, printed INSIDE the ring: the old pair of end labels sat
  // on the arc itself and the stroke swallowed them. Inside, every number
  // has dark ground behind it, and the quarters give the needle a scale to
  // be read against rather than only two ends to interpolate between.
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => {
    const [tx, ty] = point(f, r - thickness / 2 - 14);
    return { f, x: tx, y: ty + 3, v: Math.round(f * max) };
  });

  return (
    <div className="asset-gauge">
      <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`}
           role="img" aria-label={`${label}: ${value.toLocaleString()}${unit} of ${max.toLocaleString()}${unit}`}>
        {segs.map((s) => (
          <path key={s.key} d={s.d} fill="none" stroke={s.colour}
                strokeWidth={thickness} strokeLinecap="butt" />
        ))}
        {ticks.map((t) => (
          <text key={t.f} x={t.x} y={t.y} textAnchor="middle"
                className="asset-gauge-tick">{t.v.toLocaleString()}</text>
        ))}
        <line x1={cx} y1={cy} x2={nx} y2={ny} className="asset-gauge-needle" />
        <circle cx={cx} cy={cy} r={4.5} className="asset-gauge-hub" />
      </svg>
      <div className="asset-gauge-value">
        {value.toLocaleString()}{unit}
        <span className="muted"> of {max.toLocaleString()}{unit}</span>
      </div>
      <div className="asset-gauge-label">{label}</div>
    </div>
  );
}

export type Column = { label: string; n: number; colour?: string };

/** Vertical columns, for a SEQUENCE with short labels - quarters, bands.
 *
 *  The order given is the order drawn; a sequence sorted by size stops being a
 *  sequence. Each value rides its own column's top - value and bar share a
 *  bottom-anchored stack, so the figure moves with the bar it names rather
 *  than floating in a row of its own - and the baseline is zero for the same
 *  reason the bar chart's is. */
export function VColumns({ rows, format, caption }: {
  rows: Column[];
  format?: (n: number) => string;
  /** Names what the figures ARE ("Potential new items") - the one thing
   *  neither the labels nor the values say. The count is the VERTICAL
   *  dimension of a column chart, so it stands rotated on the y axis. */
  caption?: string;
}) {
  const max = Math.max(1, ...rows.map((r) => r.n));
  const { bind, tipEl } = useHoverTip();
  if (rows.length === 0) return <p className="muted">Nothing recorded.</p>;

  return (
    <div className="asset-vcols-frame">
    {caption && <div className="asset-vcols-ylabel">{caption}</div>}
    <div className="asset-vcols"
         style={{ gridTemplateColumns: `repeat(${rows.length}, 1fr)` }}>
      {rows.map((r) => (
        <div className="asset-vcols-col" key={r.label}
             {...bind(<><b>{r.label}</b>{' '}
               {format ? format(r.n) : r.n.toLocaleString()}</>)}>
          <div className="asset-col-stack">
            <div className="v">{r.n ? (format ? format(r.n) : r.n.toLocaleString()) : ''}</div>
            <div className="bar"
                 style={{ height: `${(r.n / max) * 100}%`,
                          background: r.colour }} />
          </div>
          <div className="k">{r.label}</div>
        </div>
      ))}
    </div>
    {tipEl}
    </div>
  );
}
