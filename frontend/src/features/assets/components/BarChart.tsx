import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useHoverTip } from './HoverTip';

/** A categorical bar chart: label, bar, value, and an axis under it.
 *
 *  HORIZONTAL, because the categories here are 25 device types and 23 makes
 *  with names like "OOB Management Switch". Vertical bars would rotate or
 *  truncate every one of them; horizontal ones read left to right at any
 *  length and take another category by growing downwards, which a page can
 *  scroll.
 *
 *  ONE COLOUR. The category is already named on its own row, so a hue per bar
 *  encodes nothing - it is decoration the reader will hunt for meaning in, and
 *  it fails for a colourblind viewer in exchange for nothing. Colour is spent
 *  where it means something instead: lifecycle states, and the two ends of the
 *  rack-fill histogram.
 *
 *  THE AXIS STARTS AT ZERO, ALWAYS. A bar encodes magnitude by length, so a
 *  truncated baseline makes the length lie - 63 against 104 becomes a sliver
 *  against a tower when the axis starts at 63, though it is only 1.65 times
 *  smaller.
 */

export type BarRow = {
  label: string;
  n: number;
  /** Filters the inventory to this row, when the row maps to a filter. */
  href?: string;
  /** Overrides the single accent, for series where colour carries meaning. */
  colour?: string;
  /** A denominator. With it the bar reads as a PROPORTION of its own total and
   *  the value prints "628 / 664" - which is the whole point of a completeness
   *  chart, where 628 alone says nothing. */
  of?: number;
};

const LIMITS = [10, 25, 0] as const;   // 0 = all

export function BarChart({
  rows, unit = '', limitable = true, defaultLimit = 10, sorted = true, format,
}: {
  rows: BarRow[];
  unit?: string;
  /** Renders the value. Counts are counts, but a bar can carry money or
   *  kilowatts, and "147396.08" is not how either is read. */
  format?: (n: number) => string;
  limitable?: boolean;
  defaultLimit?: number;
  /** False keeps the order given. A runway is a SEQUENCE - expired, then each
   *  quarter, then later - and sorting it by size would destroy the only thing
   *  it is for. */
  sorted?: boolean;
}) {
  const [limit, setLimit] = useState<number>(defaultLimit);
  const { bind, tipEl } = useHoverTip();

  const ranked = sorted ? [...rows].sort((a, b) => b.n - a.n) : rows;
  const capped = limit > 0 && ranked.length > limit + 1;
  const shown = capped ? ranked.slice(0, limit) : ranked;
  const hidden = capped ? ranked.slice(limit) : [];
  const hiddenTotal = hidden.reduce((t, r) => t + r.n, 0);

  // Scaled to the largest bar, not the total: at 310 of 664 every other
  // category would be a sliver, and comparing them with each other is the
  // whole job of the chart. A ratio series scales to its own denominator, so "628 of 664" and "0 of
  // 664" are drawn against the same 664 and can be compared down the column.
  const ratio = shown.length > 0 && shown.every((r) => r.of != null);
  const max = ratio
    ? Math.max(1, ...shown.map((r) => r.of ?? 0))
    : Math.max(1, ...shown.map((r) => r.n), hiddenTotal);
  const ticks = axisTicks(max);

  if (rows.length === 0) return <p className="muted">Nothing recorded.</p>;

  return (
    <div className="asset-chart">
      {limitable && ranked.length > 10 && (
        <div className="asset-chart-controls">
          <span className="muted">{ranked.length} categories</span>
          <span style={{ flex: 1 }} />
          {LIMITS.map((l) => (
            <button
              key={l}
              type="button"
              className={limit === l ? 'is-current' : ''}
              onClick={() => setLimit(l)}
            >
              {l === 0 ? 'All' : `Top ${l}`}
            </button>
          ))}
        </div>
      )}

      <div className="asset-chart-rows">
        {shown.map((r) => (
          <Bar key={r.label} row={r} max={max} unit={unit} format={format}
               bind={bind} />
        ))}
        {hidden.length > 0 && (
          // Summed rather than dropped: without it the bars no longer add up
          // to the total printed at the top of the panel, and a reader who
          // checks would find the chart wrong.
          <Bar
            row={{ label: `and ${hidden.length} others`, n: hiddenTotal }}
            max={max}
            unit={unit}
            format={format}
            bind={bind}
            muted
          />
        )}
      </div>
      {tipEl}

      <div className="asset-chart-axis" aria-hidden="true">
        <span className="lbl" />
        <div className="ticks">
          {ticks.map((t) => (
            <span key={t} style={{ left: `${(t / max) * 100}%` }}>
              {t.toLocaleString()}
            </span>
          ))}
        </div>
        <span className="val" />
      </div>
    </div>
  );
}

function Bar({ row, max, unit, muted, format, bind }: {
  row: BarRow; max: number; unit: string; muted?: boolean;
  format?: (n: number) => string;
  bind: (node: ReactNode) => Record<string, unknown>;
}) {
  const shownValue = format ? format(row.n) : `${row.n.toLocaleString()}${unit}`;
  const width = `${(row.n / max) * 100}%`;
  const label = row.href
    ? <Link to={row.href}>{row.label}</Link>
    : <span>{row.label}</span>;

  return (
    <div className={`asset-chart-row${muted ? ' is-muted' : ''}`}
         {...bind(<><b>{row.label}</b>{' '}
           {row.of != null
             ? `${row.n.toLocaleString()} of ${row.of.toLocaleString()}`
             : shownValue}</>)}>
      <div className="lbl">{label}</div>
      <div className="track">
        <span className="fill"
              style={{ width, background: row.colour }} />
      </div>
      {/* The number is always printed. A bar shows a ranking; the figure is
          what somebody quotes in a meeting, and reading it off an axis is a
          worse way to get it. */}
      <div className="val">
        {shownValue}
        {row.of != null && (
          <span className="of"> / {row.of.toLocaleString()}</span>
        )}
      </div>
    </div>
  );
}

/** Zero, the max, and a round value between - enough to judge a length by
 *  without the panel turning into graph paper. */
function axisTicks(max: number): number[] {
  const mid = niceRound(max / 2);
  return mid > 0 && mid < max ? [0, mid, max] : [0, max];
}

function niceRound(v: number): number {
  if (v <= 0) return 0;
  const mag = 10 ** Math.floor(Math.log10(v));
  return Math.round(v / mag) * mag;
}
