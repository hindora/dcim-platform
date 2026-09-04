import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useHoverTip } from '../../components/HoverTip';
import { Seg } from '../../components/estate';
import { api, type AssetTrends } from '../../api/client';

/** The windows a trend is read at. Days drive the snapshot series; the
 *  backend widens the activity chart's month window to match. */
const RANGES = [
  { key: '30', label: '30D' },
  { key: '90', label: '90D' },
  { key: '180', label: '180D' },
  { key: '365', label: '1Y' },
] as const;

/** What Item count counts. Each is a column the snapshot already carries -
 *  the picker chooses, it never refetches. */
const METRICS = [
  ['devices', 'All devices'],
  ['in_service', 'In service'],
  ['installed', 'Installed'],
  ['in_stock', 'In stock'],
  ['planned', 'Planned'],
  ['maintenance', 'Maintenance'],
] as const;
type Metric = (typeof METRICS)[number][0];

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
/** A panel heading with its own range control. Each chart reads at its own
 *  window - a year of item count beside a month of deltas is a legitimate
 *  combination - and every toggle SLICES the year already in hand rather
 *  than refetching. */
function PanelHead({ title, range, onRange }: {
  title: string; range: string; onRange: (v: string) => void;
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
      <h3 style={{ marginRight: 'auto' }}>{title}</h3>
      <Seg value={range} label={`${title} range`}
           options={RANGES.map((r) => ({ key: r.key, label: r.label }))}
           onChange={onRange} />
    </div>
  );
}

/** The last `days` entries. Snapshots are one row per night, so the count is
 *  the window. */
function lastDays<T>(rows: T[], days: string): T[] {
  return rows.slice(-Number(days));
}

export function Trends() {
  const [metric, setMetric] = useState<Metric>('devices');
  const [rItems, setRItems] = useState('90');
  const [rFree, setRFree] = useState('90');
  const [rDelta, setRDelta] = useState('90');
  const [rActivity, setRActivity] = useState('365');
  // One fetch at the widest window; the per-panel controls slice it.
  const { data, isLoading, error } = useQuery<AssetTrends>({
    queryKey: ['asset-trends'],
    queryFn: () => api.assetTrends(365),
    refetchInterval: 5 * 60_000,
  });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (isLoading || !data) return null;

  const snaps = data.snapshots;
  const deltaSnaps = lastDays(snaps, rDelta);
  const deltas = deltaSnaps.slice(1).map((s, i) => ({
    label: s.day.slice(5),
    n: s.u_used - deltaSnaps[i].u_used,
  }));
  const activity = data.activity.slice(
    -Math.max(1, Math.round(Number(rActivity) / 30)));

  return (
    <>
      <h3 className="asset-charts-head">Trends</h3>
      {/* Two-up: a trend line squeezed to a quarter of the row flattens the
          movement it exists to show. Half the row gives the day-to-day slope
          room to be read. */}
      <div className="asset-cols asset-cols-two">
        {/* asset-panel-flex + asset-trend-slot: the panels are equal height
            by grid, but the content above each chart is not (the metric
            picker), so the charts floated at different heights. The slot
            takes margin-top auto and every baseline lands the same distance
            from its panel's bottom. */}
        <div className="asset-panel asset-panel-flex">
          <PanelHead title="Item count" range={rItems} onRange={setRItems} />
          <div className="asset-panel-filters">
            <select value={metric} aria-label="Metric"
                    onChange={(e) => setMetric(e.target.value as Metric)}>
              {METRICS.map(([key, label]) => (
                <option key={key} value={key}>{label}</option>
              ))}
            </select>
          </div>
          <div className="asset-trend-slot">
            <TrendLine
              points={lastDays(snaps, rItems).map((s) => ({ day: s.day, v: s[metric] }))}
            />
          </div>
        </div>

        <div className="asset-panel asset-panel-flex">
          <PanelHead title="Free rack units" range={rFree} onRange={setRFree} />
          <div className="asset-trend-slot">
            <TrendLine
              points={lastDays(snaps, rFree).map((s) => ({ day: s.day, v: s.u_free }))}
              unit="U"
            />
          </div>
        </div>

        <div className="asset-panel asset-panel-flex">
          <PanelHead title="Rack units delta" range={rDelta} onRange={setRDelta} />
          {/* Day-on-day change in INSTALLED units, from consecutive snapshots.
              Needs two nights by definition - a delta is a difference. */}
          <div className="asset-trend-slot">
            {deltas.length === 0 ? (
              <p className="muted">
                Needs two nightly snapshots — the first difference appears
                tomorrow.
              </p>
            ) : (
              <Diverging rows={deltas} unit="U" />
            )}
          </div>
        </div>

        <div className="asset-panel asset-panel-flex">
          <PanelHead title="Installs and decommissions"
                     range={rActivity} onRange={setRActivity} />
          {/* From the lifecycle events, NOT snapshot diffs: ten installs and
              ten decommissions in one month net to zero, and the activity is
              the point. */}
          <div className="asset-trend-slot">
            {activity.length === 0 ? (
              <p className="muted">
                Accrues as assets move through lifecycle states.
              </p>
            ) : (
              <Paired
                rows={activity.map((a) => ({
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
