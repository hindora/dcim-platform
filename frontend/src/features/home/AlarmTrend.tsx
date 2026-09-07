/** The raised-over-time chart, and the glyph that opens it.
 *
 *  One chart for every place that asks "is now normal here": the alarm
 *  panel's history tab (whole scope and per room) and the room drawer.
 *  Counted by the server per day or per week; see /estate/alarm-trend.
 */

import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type AlarmCategory } from '../../api/client';
import { useHoverTip } from '../../components/HoverTip';

/** What the chart is about: the estate when absent, else one site or room. */
export interface TrendScope { kind: 'site' | 'room'; id: string; label: string }

/** Every category at once - the room drawer's question is about the room,
 *  not one domain of it. */
export const ALL_CATEGORIES: AlarmCategory[] = [
  'visibility', 'environmental', 'cooling', 'power',
  'it_equipment', 'network', 'capacity',
];

/** A rising line with its arrowhead: the history table's per-room trend
 *  toggle. Drawn inline so it takes the button's colour in both themes. */
export function TrendGlyph() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none"
         stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"
         strokeLinejoin="round" aria-hidden>
      <polyline points="1.5,12.5 5.5,7.5 8.5,10 14.5,3.5" />
      <polyline points="10.5,3.5 14.5,3.5 14.5,7.5" />
    </svg>
  );
}

/** Conditions raised per day over the last fortnight, in one scope. The
 *  table beside it says what is happening; this says whether that is normal
 *  - a hall raising six a day for two weeks and a hall that just started are
 *  different problems wearing the same count.
 *
 *  Counted by the server. This used to bucket a page of the alarm list in
 *  the browser, and that page is capped at 500 and ordered by severity: at
 *  estate scope it charted the 500 most severe conditions ever, not the
 *  fortnight. */
/** How far back a trend looks. Daily bars up to a quarter; beyond that a
 *  bar is a week, because 180 daily columns is a texture, not a chart. */
const RANGES = [
  { label: '30D', days: 30, bucket: 'day', said: 'the last 30 days' },
  { label: '90D', days: 90, bucket: 'day', said: 'the last 90 days' },
  { label: '180D', days: 180, bucket: 'week', said: 'the last 180 days' },
  { label: '1Y', days: 365, bucket: 'week', said: 'the last year' },
] as const;
type Range = typeof RANGES[number];

export function AlarmTrend({ categories, scope }: {
  categories: AlarmCategory[]; scope?: TrendScope;
}) {
  const { bind, tipEl } = useHoverTip();
  const [range, setRange] = useState<Range>(RANGES[0]);
  // How wide the chart actually is. The same 30 bars are readable across a
  // full-width sheet and a wall of dates in an 800px drawer, so what to hide
  // is decided from pixels per bar, not from the bar count.
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      setWidth(entries[0]?.contentRect.width ?? 0);
    });
    ro.observe(el);
    setWidth(el.getBoundingClientRect().width);
    return () => ro.disconnect();
  }, []);
  const { data, error } = useQuery({
    queryKey: ['alarm-trend', scope?.kind ?? '', scope?.id ?? '',
               range.days, range.bucket, ...categories],
    queryFn: () => api.alarmTrend(categories, {
      room: scope?.kind === 'room' ? scope.id : undefined,
      site: scope?.kind === 'site' ? scope.id : undefined,
    }, range.days, range.bucket),
    staleTime: 60_000,
    // Keep the old bars up while the new range loads: a chart that blanks
    // on every click reads as broken, and the ranges are compared by eye.
    placeholderData: (prev) => prev,
  });

  const picker = (
    <div className="trend-range" role="group" aria-label="How far back">
      {RANGES.map((r) => (
        <button key={r.label} className={`sort ${range === r ? 'on' : ''}`}
                aria-pressed={range === r} onClick={() => setRange(r)}>
          {r.label}
        </button>
      ))}
    </div>
  );

  if (error) {
    return <div ref={box}>{picker}<p className="muted">Could not load the trend.</p></div>;
  }
  if (!data) {
    return <div ref={box}>{picker}<p className="muted">Loading the trend…</p></div>;
  }

  const points = data.points;
  const max = Math.max(1, ...points.map((p) => p.raised));
  const total = data.total;
  const weekly = data.bucket === 'week';
  // Pixels per bar decide what fits. A value needs ~34px before it collides
  // with its neighbour; a date label ~46px. Below that the values move into
  // the tooltip and the axis keeps every k-th date. Until the box has been
  // measured, assume room - a first paint that hides everything and then
  // shows it flickers.
  const per = width > 0 ? width / Math.max(1, points.length) : 999;
  const dense = per < 34;
  const every = Math.max(1, Math.ceil(46 / per));
  const unit = weekly ? 'week' : 'day';

  return (
    <div ref={box}>
      {picker}
      <p className="muted">
        {total} condition{total === 1 ? '' : 's'} raised in {range.said}
        {scope ? ` in ${scope.label}` : ' on the estate'}
        {weekly ? ', by week' : ''}.
      </p>
      <div className={`alarm-trend ${dense ? 'dense' : ''}`} role="img"
           aria-label={`Conditions raised per ${unit}, ${range.said}`}>
        {points.map(({ day, raised: n }, i) => (
          <div className="col" key={day}
               {...bind(<><b>{weekly ? `w/c ${day.slice(5)}` : day.slice(5)}</b> {n} raised</>)}>
            <div className="barwrap">
              <div className="v">{n || ''}</div>
              <div className="bar" style={{ height: `${(n / max) * 100}%` }} />
            </div>
            <div className={`k ${dense && i % every !== 0 ? 'hide' : ''}`}>
              {day.slice(5)}
            </div>
          </div>
        ))}
        {tipEl}
      </div>
    </div>
  );
}
