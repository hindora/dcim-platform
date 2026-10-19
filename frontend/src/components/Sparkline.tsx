/** A trend in the width of a word.
 *
 *  One line, no axes, no labels: the number beside it is the reading, this
 *  is only whether it has been climbing, falling or flat. The range is in the
 *  tooltip for whoever wants it. Drawn in the accent, never in a status hue,
 *  because a sparkline has no threshold to be red against.
 *
 *  `points` are [epoch_ms, value] pairs as the history API returns them. The
 *  vertical scale is the series' own min to max with a little headroom, so a
 *  flat line stays flat and a 0.3 °C wobble is not drawn as a cliff: a span
 *  under `floor` units is widened to `floor`, centred on the data. */
export function Sparkline({ points, floor = 1, unit = '', height = 22, className }: {
  points: number[][];
  /** The smallest span the vertical axis may show, in the series' unit. */
  floor?: number;
  unit?: string;
  height?: number;
  className?: string;
}) {
  const vals = points.map((p) => p[1]).filter((v): v is number => typeof v === 'number' && Number.isFinite(v));
  if (vals.length < 2) {
    return <span className={`spark is-empty${className ? ` ${className}` : ''}`} aria-hidden style={{ height }} />;
  }
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi - lo < floor) { const mid = (hi + lo) / 2; lo = mid - floor / 2; hi = mid + floor / 2; }
  const pad = (hi - lo) * 0.12;
  lo -= pad; hi += pad;
  const t0 = points[0][0], t1 = points[points.length - 1][0] || t0 + 1;
  const W = 100, H = 24;
  const xy = points
    .filter((p) => typeof p[1] === 'number' && Number.isFinite(p[1]))
    .map((p) => [((p[0] - t0) / Math.max(1, t1 - t0)) * W, H - ((p[1] - lo) / (hi - lo)) * H]);
  const line = xy.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(2)} ${y.toFixed(2)}`).join(' ');
  const area = `${line} L${xy[xy.length - 1][0].toFixed(2)} ${H} L${xy[0][0].toFixed(2)} ${H} Z`;
  const fmt = (v: number) => `${Number.isInteger(v) ? v : v.toFixed(1)}${unit ? ` ${unit}` : ''}`;
  const first = vals[0], last = vals[vals.length - 1];
  const drift = last - first;
  const word = Math.abs(drift) < floor / 4 ? 'steady' : drift > 0 ? 'rising' : 'falling';
  return (
    <svg className={`spark${className ? ` ${className}` : ''}`} viewBox={`0 0 ${W} ${H}`}
         preserveAspectRatio="none" height={height} role="img"
         aria-label={`${word} over the last hour, ${fmt(Math.min(...vals))} to ${fmt(Math.max(...vals))}`}>
      <title>{`${word} · last hour ${fmt(Math.min(...vals))} to ${fmt(Math.max(...vals))}`}</title>
      <path className="spark-area" d={area} />
      <path className="spark-line" d={line} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
