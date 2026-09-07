/** The raised-over-time chart, and the glyph that opens it.
 *
 *  One chart for every place that asks "is now normal here": the alarm
 *  panel's history tab (whole scope and per room) and the room and site
 *  drawers. Counted by the server per day or per week; see
 *  /estate/alarm-trend.
 *
 *  Its form is the Assets page's vertical-columns chart: a SEQUENCE with
 *  short labels, values riding the column tops, a zero baseline, the
 *  y-axis named beside it, and the range chosen with the same segmented
 *  control the asset trends use - one vocabulary across the product, so a
 *  chart here reads as the same kind of thing as a chart there.
 */

import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type AlarmCategory, type AlarmTrend as TrendData } from '../../api/client';
import { Seg } from '../../components/estate';
import { useHoverTip } from '../../components/HoverTip';
import { MaxGlyph, MaxModal } from '../../components/MaxModal';

/** Whether a maximized chart is up. The sheet and the drawers close on
 *  Escape from a window listener of their own; while a modal is open the
 *  key is the modal's, or one press would take both layers down. */
export const maxOpen = () => document.querySelector('.max-modal') !== null;

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
 *  bar is a week, because 180 daily columns is a texture, not a chart.
 *  CUSTOM is the fifth cell: two dates the operator picks - a change
 *  freeze, an incident week - honoured to the day. */
const RANGES = [
  { label: '30D', days: 30, bucket: 'day', said: 'in the last 30 days' },
  { label: '90D', days: 90, bucket: 'day', said: 'in the last 90 days' },
  { label: '180D', days: 180, bucket: 'week', said: 'in the last 180 days' },
  { label: '1Y', days: 365, bucket: 'week', said: 'in the last year' },
] as const;
type Range = typeof RANGES[number];
const CUSTOM = 'CUSTOM';
/** Past this many picked days a bar is a week, as for the presets. */
const WEEKLY_FROM_DAYS = 120;
const MAX_DAYS = 366;

const isoDay = (d: Date) => d.toISOString().slice(0, 10);
const daysBetween = (a: string, b: string) =>
  Math.round((Date.parse(b) - Date.parse(a)) / 86_400_000) + 1;
const shortDay = (iso: string) => iso.slice(5);

/** The columns themselves, sized to the box they are in.
 *
 *  Its own component because the same points are drawn twice when the
 *  chart is maximized - inline and in the modal - and each drawing has to
 *  measure its own width: the same 30 bars are readable across a
 *  full-width sheet and a wall of dates in an 800px drawer, so what to
 *  hide is decided from pixels per bar, not from the bar count. */
function TrendBars({ data, said }: { data: TrendData; said: string }) {
  const { bind, tipEl } = useHoverTip();
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

  const points = data.points;
  const max = Math.max(1, ...points.map((p) => p.raised));
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
    <div className="alarm-trend-frame" ref={box}>
      <div className="alarm-trend-ylabel">Conditions raised</div>
      <div className={`alarm-trend ${dense ? 'dense' : ''}`} role="img"
           aria-label={`Conditions raised per ${unit}, ${said}`}>
        {points.map(({ day, raised: n }, i) => (
          <div className="col" key={day}
               {...bind(<><b>{weekly ? `w/c ${day.slice(5)}` : day.slice(5)}</b>{' '}
                 {n.toLocaleString()} raised</>)}>
            <div className="barwrap">
              <div className="v">{n ? n.toLocaleString() : ''}</div>
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

export function AlarmTrend({ categories, scope }: {
  categories: AlarmCategory[]; scope?: TrendScope;
}) {
  const [range, setRange] = useState<Range>(RANGES[0]);
  // The full-window view. It renders the same head and the same bars from
  // the same state, so a range chosen in either place is the one range and
  // the big view is never a stale copy - the asset panels' rule.
  const [maxed, setMaxed] = useState(false);
  // The picked window. `custom` says which of the two the chart is
  // reading; the dates persist while a preset is chosen, so flipping back
  // to CUSTOM returns to the last window picked rather than to today.
  const [custom, setCustom] = useState(false);
  const [since, setSince] = useState(() => {
    const d = new Date(); d.setDate(d.getDate() - 29); return isoDay(d);
  });
  const [until, setUntil] = useState(() => isoDay(new Date()));
  const span = daysBetween(since, until);
  const windowOk = span >= 1 && span <= MAX_DAYS;
  const bucket: 'day' | 'week' = custom
    ? (span > WEEKLY_FROM_DAYS ? 'week' : 'day')
    : range.bucket;
  const { data, error } = useQuery({
    queryKey: ['alarm-trend', scope?.kind ?? '', scope?.id ?? '',
               custom ? `${since}..${until}` : range.days, bucket, ...categories],
    queryFn: () => api.alarmTrend(categories, {
      room: scope?.kind === 'room' ? scope.id : undefined,
      site: scope?.kind === 'site' ? scope.id : undefined,
    }, range.days, bucket, custom ? { since, until } : undefined),
    // A window the server would refuse is not asked: the row under the
    // chips says what is wrong with it instead.
    enabled: !custom || windowOk,
    staleTime: 60_000,
    // Keep the old bars up while the new range loads: a chart that blanks
    // on every click reads as broken, and the ranges are compared by eye.
    placeholderData: (prev) => prev,
  });

  // The same segmented control the asset trend panels wear, on the title
  // row, right-aligned: a range is a slice of one series, not a filter on
  // its dimensions, and the Assets page settled what that looks like.
  const picker = (
    <Seg value={custom ? CUSTOM : range.label} label="How far back"
         options={[...RANGES.map((r) => ({ key: r.label, label: r.label })),
                   { key: CUSTOM, label: CUSTOM }]}
         onChange={(k) => {
           if (k === CUSTOM) { setCustom(true); return; }
           setCustom(false);
           setRange(RANGES.find((r) => r.label === k) ?? RANGES[0]);
         }} />
  );
  // Typing a date is choosing CUSTOM; nobody picks a day and then wants
  // to click a fifth chip to be shown it.
  const pick = (set: (v: string) => void) => (e: React.ChangeEvent<HTMLInputElement>) => {
    set(e.target.value);
    setCustom(true);
  };
  const dates = custom && (
    <div className="alarm-trend-dates">
      <label>From <input type="date" value={since} max={until} onChange={pick(setSince)} /></label>
      <label>To <input type="date" value={until} min={since} max={isoDay(new Date())}
                       onChange={pick(setUntil)} /></label>
      {!windowOk && (
        <span className="why">
          {span < 1 ? 'The end is before the start.' : `At most ${MAX_DAYS} days.`}
        </span>
      )}
    </div>
  );
  const where = scope ? `in ${scope.label}` : 'on the estate';
  const title = `Conditions raised ${where}`;
  // The head row: the caption, the range control, and - inline only - the
  // maximize glyph the asset panels carry in the same corner.
  const head = (caption: React.ReactNode, inline: boolean) => (
    <>
      <div className="alarm-trend-head">
        <p className="muted">{caption}</p>
        {picker}
        {inline && (
          <button type="button" className="asset-max"
                  aria-label={`Maximize ${title}`} title="Maximize"
                  onClick={() => setMaxed(true)}><MaxGlyph /></button>
        )}
      </div>
      {dates}
    </>
  );
  const said = custom
    ? `from ${shortDay(since)} to ${shortDay(until)}`
    : range.said;

  const caption = error ? 'Could not load the trend.'
    : custom && !windowOk ? 'Pick a window to draw.'
      : !data ? 'Loading the trend…'
        : <>
            <b>{data.total.toLocaleString()}</b> condition{data.total === 1 ? '' : 's'}
            {' '}raised {said} {where}
            {data.bucket === 'week' ? ', by week' : ''}
          </>;
  const drawable = !error && data && !(custom && !windowOk);

  return (
    <div>
      {head(caption, true)}
      {drawable && <TrendBars data={data} said={said} />}
      {maxed && (
        <MaxModal title={title} onClose={() => setMaxed(false)}>
          {head(caption, false)}
          {drawable && <TrendBars data={data} said={said} />}
        </MaxModal>
      )}
    </div>
  );
}
