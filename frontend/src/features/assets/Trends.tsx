import { useQuery } from '@tanstack/react-query';
import { useHoverTip } from '../../components/HoverTip';
import { api, type AssetTrends } from '../../api/client';

/** The estate over time, from the nightly snapshots and the lifecycle events.
 *
 *  Every panel here is honest about how much history exists. One snapshot is a
 *  dot and a sentence, not a line - a line through one point says something it
 *  cannot know - and the activity chart says it accrues rather than drawing an
 *  empty axis. These panels fill themselves in as nights pass; nothing needs
 *  revisiting.
 *
 *  Lines auto-range where bars must not: a bar encodes magnitude by LENGTH, a
 *  line by POSITION and slope, and 664 devices drawn on a zero axis is a flat
 *  stripe at the top that hides the one thing a trend exists to show. The
 *  range is printed on the axis so the zoom is never a secret.
 */
export function Trends() {
  const { data, isLoading, error } = useQuery<AssetTrends>({
    queryKey: ['asset-trends'],
    queryFn: api.assetTrends,
    refetchInterval: 5 * 60_000,
  });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (isLoading || !data) return null;

  const snaps = data.snapshots;
  const deltas = snaps.slice(1).map((s, i) => ({
    label: s.day.slice(5),
    n: s.u_used - snaps[i].u_used,
  }));

  return (
    <>
      <h3 className="asset-charts-head">Over time</h3>
      {/* Two-up: a trend line squeezed to a quarter of the row flattens the
          movement it exists to show. Half the row gives the day-to-day slope
          room to be read. */}
      <div className="asset-cols asset-cols-two">
        <div className="asset-panel">
          <h3>Item count</h3>
          <TrendLine
            points={snaps.map((s) => ({ day: s.day, v: s.devices }))}
          />
        </div>

        <div className="asset-panel">
          <h3>Free rack units</h3>
          <TrendLine
            points={snaps.map((s) => ({ day: s.day, v: s.u_free }))}
            unit="U"
          />
        </div>

        <div className="asset-panel">
          <h3>Rack units delta</h3>
          {/* Day-on-day change in INSTALLED units, from consecutive snapshots.
              Needs two nights by definition - a delta is a difference. */}
          {deltas.length === 0 ? (
            <p className="muted">
              Needs two nightly snapshots — the first difference appears
              tomorrow.
            </p>
          ) : (
            <Diverging rows={deltas} unit="U" />
          )}
        </div>

        <div className="asset-panel">
          <h3>Installs and decommissions</h3>
          {/* From the lifecycle events, NOT snapshot diffs: ten installs and
              ten decommissions in one month net to zero, and the activity is
              the point. */}
          {data.activity.length === 0 ? (
            <p className="muted">
              Accrues as assets move through lifecycle states.
            </p>
          ) : (
            <Paired
              rows={data.activity.map((a) => ({
                label: a.month.slice(2),
                a: a.installs,
                b: a.decommissions,
              }))}
              aLabel="Installs"
              bLabel="Decommissions"
            />
          )}
        </div>
      </div>
    </>
  );
}

function TrendLine({ points, unit = '' }: {
  points: { day: string; v: number }[];
  unit?: string;
}) {
  if (points.length === 0) {
    return <p className="muted">No snapshots yet.</p>;
  }
  const last = points[points.length - 1];
  if (points.length === 1) {
    return (
      <div className="asset-trend-single">
        <div className="v">{last.v.toLocaleString()}{unit}</div>
        <p className="muted">
          Recording since {last.day} — the line appears as days accrue.
        </p>
      </div>
    );
  }

  const w = 260;
  const h = 90;
  const pad = 4;
  const vs = points.map((p) => p.v);
  const lo = Math.min(...vs);
  const hi = Math.max(...vs);
  const span = Math.max(1, hi - lo);
  const x = (i: number) => pad + (i / (points.length - 1)) * (w - 2 * pad);
  const y = (v: number) => h - pad - ((v - lo) / span) * (h - 2 * pad);
  const path = points.map((p, i) =>
    `${i === 0 ? 'M' : 'L'} ${x(i).toFixed(1)} ${y(p.v).toFixed(1)}`).join(' ');

  return (
    <div className="asset-trend">
      <svg width="100%" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none"
           role="img"
           aria-label={`${points.length} days, from ${points[0].v} to ${last.v}${unit}`}>
        <path d={`${path} L ${x(points.length - 1)} ${h - pad} L ${x(0)} ${h - pad} Z`}
              className="asset-trend-area" />
        <path d={path} className="asset-trend-line" />
        <circle cx={x(points.length - 1)} cy={y(last.v)} r="3"
                className="asset-trend-dot" />
      </svg>
      <div className="asset-trend-axis">
        {/* The range is printed because the line auto-ranges: a zoomed axis
            with its bounds visible is honest, a secret one is not. */}
        <span>{points[0].day.slice(5)}</span>
        <span className="range">
          {lo.toLocaleString()}–{hi.toLocaleString()}{unit}
        </span>
        <span>{last.day.slice(5)} · <strong>{last.v.toLocaleString()}{unit}</strong></span>
      </div>
    </div>
  );
}

/** Columns diverging around zero, for a signed delta - reference 23's form.
 *  Positive grows up in the accent, negative down in the warning colour, and
 *  the shared baseline is the zero every length is measured from. */
function Diverging({ rows, unit }: {
  rows: { label: string; n: number }[];
  unit: string;
}) {
  const max = Math.max(1, ...rows.map((r) => Math.abs(r.n)));
  const { bind, tipEl } = useHoverTip();
  return (
    <div className="asset-diverge"
         style={{ gridTemplateColumns: `repeat(${rows.length}, 1fr)` }}>
      {rows.map((r, i) => (
        <div className="asset-diverge-col" key={`${r.label}-${i}`}
             {...bind(<><b>{r.label}</b> {r.n > 0 ? '+' : ''}{r.n}{unit}</>)}>
          <div className="up">
            {r.n > 0 && (
              <div className="bar is-up"
                   style={{ height: `${(r.n / max) * 100}%` }} />
            )}
          </div>
          <div className="down">
            {r.n < 0 && (
              <div className="bar is-down"
                   style={{ height: `${(-r.n / max) * 100}%` }} />
            )}
          </div>
          <div className="k">{r.label}</div>
        </div>
      ))}
      {tipEl}
    </div>
  );
}

/** Two columns per bucket - reference 24's form. The pair shares one scale, or
 *  installs and decommissions could not be compared against each other, which
 *  is the only reason to draw them together. */
function Paired({ rows, aLabel, bLabel }: {
  rows: { label: string; a: number; b: number }[];
  aLabel: string;
  bLabel: string;
}) {
  const max = Math.max(1, ...rows.flatMap((r) => [r.a, r.b]));
  const { bind, tipEl } = useHoverTip();
  return (
    <div className="asset-paired-wrap">
      <div className="asset-legend">
        <span><span className="sw" style={{ background: 'var(--accent)' }} />{aLabel}</span>
        <span><span className="sw" style={{ background: 'var(--warn)' }} />{bLabel}</span>
      </div>
      <div className="asset-paired"
           style={{ gridTemplateColumns: `repeat(${rows.length}, 1fr)` }}>
        {rows.map((r) => (
          <div className="asset-paired-col" key={r.label}
               {...bind(<><b>{r.label}</b> {r.a} in, {r.b} out</>)}>
            <div className="bars">
              <div className="bar is-a" style={{ height: `${(r.a / max) * 100}%` }} />
              <div className="bar is-b" style={{ height: `${(r.b / max) * 100}%` }} />
            </div>
            <div className="k">{r.label}</div>
          </div>
        ))}
      </div>
      {tipEl}
    </div>
  );
}
