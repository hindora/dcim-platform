import { useId, useLayoutEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useHoverTip } from '../../components/HoverTip';
import { Seg } from '../../components/estate';
import { MaxGlyph, MaxModal } from '../../components/MaxModal';
import { api, type AssetTrends } from '../../api/client';
import { toCss } from '../../lib/zoom';
import { Notice } from '../../components/Notice';

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
/** A trend panel: its own range control, and a maximize into the full-window
 *  modal. Each chart reads at its own window - a year of item count beside a
 *  month of deltas is a legitimate combination - and every toggle SLICES the
 *  year already in hand rather than refetching. The modal renders the same
 *  filter and chart elements, so state chosen in either place is one state. */
function TrendPanel({ title, range, onRange, filters, children }: {
  title: string; range: string; onRange: (v: string) => void;
  filters?: React.ReactNode; children: React.ReactNode;
}) {
  const [max, setMax] = useState(false);
  return (
    <div className="asset-panel asset-panel-flex">
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <h3 style={{ marginRight: 'auto' }}>{title}</h3>
        <Seg value={range} label={`${title} range`}
             options={RANGES.map((r) => ({ key: r.key, label: r.label }))}
             onChange={onRange} />
        <button type="button" className="asset-max"
                aria-label={`Maximize ${title}`}
                onClick={() => setMax(true)}><MaxGlyph /></button>
      </div>
      {filters}
      <div className="asset-trend-slot">{children}</div>
      {max && (
        <MaxModal title={title} onClose={() => setMax(false)}>
          {filters}
          {children}
        </MaxModal>
      )}
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

  if (error) return <Notice tone="error">Failed to load: {String(error)}</Notice>;
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
        <TrendPanel title="Item count" range={rItems} onRange={setRItems}
                    filters={
                      <div className="asset-panel-filters">
                        <select value={metric} aria-label="Metric"
                                onChange={(e) => setMetric(e.target.value as Metric)}>
                          {METRICS.map(([key, label]) => (
                            <option key={key} value={key}>{label}</option>
                          ))}
                        </select>
                      </div>
                    }>
          <TrendLine
            points={lastDays(snaps, rItems).map((s) => ({ day: s.day, v: s[metric] }))}
            label={METRICS.find(([k]) => k === metric)?.[1] ?? 'Item count'}
          />
        </TrendPanel>

        <TrendPanel title="Free rack units" range={rFree} onRange={setRFree}>
          <TrendLine
            points={lastDays(snaps, rFree).map((s) => ({ day: s.day, v: s.u_free }))}
            unit="U" label="Free rack units"
          />
        </TrendPanel>

        {/* Day-on-day change in INSTALLED units, from consecutive snapshots.
            Needs two nights by definition - a delta is a difference. */}
        <TrendPanel title="Rack units delta" range={rDelta} onRange={setRDelta}>
          {deltas.length === 0 ? (
            <p className="muted">
              Needs two nightly snapshots — the first difference appears
              tomorrow.
            </p>
          ) : (
            <Diverging rows={deltas} unit="U" />
          )}
        </TrendPanel>

        {/* From the lifecycle events, NOT snapshot diffs: ten installs and
            ten decommissions in one month net to zero, and the activity is
            the point. */}
        <TrendPanel title="Installs and decommissions"
                    range={rActivity} onRange={setRActivity}>
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
        </TrendPanel>
      </div>
    </>
  );
}

/** A day as the tooltip and the axis print it: "21 Oct", not "10-21". */
function dayLabel(day: string): string {
  const d = new Date(`${day}T00:00:00Z`);
  return Number.isNaN(d.getTime()) ? day
    : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
}

/** A nightly series as a line you can read point by point.
 *
 *  Hover - or focus and use the arrow keys - and a crosshair snaps to the
 *  nearest REAL snapshot, never an interpolated one, with the night, its value
 *  and the change from the night before. The line auto-ranges, so the faint
 *  grid carries the low, middle and high values and the axis prints the range.
 *
 *  Drawn at its real pixel width rather than a stretched viewBox: stretching
 *  squashed the end dot into an ellipse and thickened the line unevenly. */
function TrendLine({ points, unit = '', label }: {
  points: { day: string; v: number }[];
  unit?: string;
  /** What the series is, for the tooltip and the accessible name. */
  label: string;
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const svg = useRef<SVGSVGElement>(null);
  const [w, setW] = useState(320);
  const [at, setAt] = useState<number | null>(null);
  const [tipAt, setTipAt] = useState<{ x: number; y: number } | null>(null);
  const gid = useId();
  const hasLine = points.length > 1;

  useLayoutEffect(() => {
    const el = wrap.current;
    if (!el) return undefined;
    const ro = new ResizeObserver(() => setW(Math.max(160, Math.round(el.clientWidth))));
    ro.observe(el);
    setW(Math.max(160, Math.round(el.clientWidth)));
    return () => ro.disconnect();
  }, [hasLine]);

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

  const h = 120;
  const padX = 6, padT = 10, padB = 8;
  const vs = points.map((p) => p.v);
  const lo = Math.min(...vs);
  const hi = Math.max(...vs);
  const span = Math.max(1, hi - lo);
  const x = (i: number) => padX + (i / (points.length - 1)) * (w - 2 * padX);
  const y = (v: number) => h - padB - ((v - lo) / span) * (h - padT - padB);
  const line = points.map((p, i) =>
    `${i === 0 ? 'M' : 'L'} ${x(i).toFixed(1)} ${y(p.v).toFixed(1)}`).join(' ');
  const area = `${line} L ${x(points.length - 1).toFixed(1)} ${h - padB} L ${x(0).toFixed(1)} ${h - padB} Z`;
  const grid = hi === lo ? [lo] : [hi, (hi + lo) / 2, lo];

  /** Show the sample at index i, with the tip pinned to where it is drawn. */
  const show = (i: number) => {
    const box = svg.current?.getBoundingClientRect();
    setAt(i);
    if (box) {
      const sx = box.width / w, sy = box.height / h;
      setTipAt({ x: toCss(box.left + x(i) * sx), y: toCss(box.top + y(points[i].v) * sy) });
    }
  };
  const onMove = (e: React.MouseEvent<SVGRectElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    if (!box.width) return;
    const frac = (e.clientX - box.left) / box.width;
    show(Math.max(0, Math.min(points.length - 1, Math.round(frac * (points.length - 1)))));
  };
  const onKey = (e: React.KeyboardEvent<SVGSVGElement>) => {
    const step: Record<string, number> = { ArrowLeft: -1, ArrowRight: 1, Home: -Infinity, End: Infinity };
    if (!(e.key in step)) return;
    e.preventDefault();
    const cur = at ?? points.length - 1;
    const next = step[e.key] === -Infinity ? 0 : step[e.key] === Infinity ? points.length - 1
      : Math.max(0, Math.min(points.length - 1, cur + step[e.key]));
    show(next);
  };
  const hide = () => { setAt(null); setTipAt(null); };

  const p = at !== null ? points[at] : null;
  const prev = at !== null && at > 0 ? points[at - 1] : null;
  const delta = p && prev ? p.v - prev.v : null;
  const fmt = (v: number) => `${v.toLocaleString()}${unit ? ` ${unit}` : ''}`;

  return (
    <div className="asset-trend" ref={wrap}>
      <svg ref={svg} width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="asset-trend-svg"
           role="img" tabIndex={0}
           aria-label={`${label}: ${points.length} nights, ${points[0].v.toLocaleString()} to ${fmt(last.v)}, ranging ${lo.toLocaleString()} to ${fmt(hi)}. Use the arrow keys to read each night.`}
           onKeyDown={onKey} onBlur={hide}>
        <defs>
          <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" className="asset-trend-grad-top" />
            <stop offset="100%" className="asset-trend-grad-bottom" />
          </linearGradient>
        </defs>
        {grid.map((g) => (
          <line key={g} x1={padX} x2={w - padX} y1={y(g)} y2={y(g)} className="asset-trend-grid" />
        ))}
        <path d={area} fill={`url(#${gid})`} className="asset-trend-area" />
        <path d={line} className="asset-trend-line" />
        {p === null && (
          <circle cx={x(points.length - 1)} cy={y(last.v)} r="3.5" className="asset-trend-dot" />
        )}
        {p !== null && at !== null && (
          <g pointerEvents="none">
            <line className="asset-trend-cross" x1={x(at)} x2={x(at)} y1={padT - 4} y2={h - padB} />
            <circle cx={x(at)} cy={y(p.v)} r="4.5" className="asset-trend-dot is-hover" />
          </g>
        )}
        {/* Last, so it takes the pointer. */}
        <rect x={0} y={0} width={w} height={h} fill="transparent"
              onMouseMove={onMove} onMouseLeave={hide} />
      </svg>
      {p && tipAt && (
        <span className="hover-tip" role="tooltip"
              style={{ top: tipAt.y + 14, left: Math.min(tipAt.x + 12, toCss(window.innerWidth) - 230) }}>
          <b>{dayLabel(p.day)}</b> {fmt(p.v)}
          {delta !== null && (
            <span className="asset-trend-delta">
              {' '}· {delta === 0 ? 'no change' : `${delta > 0 ? '+' : '−'}${Math.abs(delta).toLocaleString()}${unit ? ` ${unit}` : ''} on the night before`}
            </span>
          )}
          <span className="asset-trend-tip-k">{label}</span>
        </span>
      )}
      <div className="asset-trend-axis">
        {/* The range is printed because the line auto-ranges: a zoomed axis
            with its bounds visible is honest, a secret one is not. */}
        <span>{dayLabel(points[0].day)}</span>
        <span className="range">
          {lo.toLocaleString()}–{hi.toLocaleString()}{unit}
        </span>
        <span>{dayLabel(last.day)} · <strong>{last.v.toLocaleString()}{unit}</strong></span>
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
