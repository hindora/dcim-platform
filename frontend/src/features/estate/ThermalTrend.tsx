/** Intake over time for the thermal page's current scope.
 *
 *  The table's Δ columns compare two windows and nothing more. This is the
 *  run-up: average, p90 and max per hour or per day, drawn against the
 *  ASHRAE band, so "warm" has a shape - a hall drifting up over a week and
 *  a hall that spiked this morning are different problems wearing the same
 *  Max.
 *
 *  Same readings as the table (rack probe first, servers as fallback), same
 *  vocabulary as every other chart: the Seg range control the asset trends
 *  and the alarm trend wear, a CUSTOM cell with two dates, the maximize
 *  glyph, values in a crosshair tip snapped to a real bucket. A day of
 *  hourly points reads the five-minute rollup, anything longer the hourly
 *  one, so the p90 is over one value per sensor per five minutes or per
 *  hour - the time-weighted basis - and can sit a little off the table's
 *  reading-level p90.
 *
 *  Under it, a second panel over the same window and the same sensors:
 *  COMPLIANCE, the table's four-way spread bar given a time axis. The two
 *  are read AGAINST each other - "it got warm on Tuesday" and "Tuesday cost
 *  four per cent of the band" are one finding in two charts - so they are
 *  two panels on the page rather than two views behind a toggle, which
 *  turned the comparison into an act of memory and hid the compliance chart
 *  from anyone who did not already know it was there.
 *
 *  ONE range control, on the intake panel, driving both: `useTrendRange`
 *  holds the window and hands each panel the same grid. Two controls would
 *  let the panels drift apart, and two charts of one scope over two
 *  different weeks is worse than either alone. They cannot share a chart -
 *  degrees and per cent on one y axis is the second-axis rule every other
 *  chart here keeps.
 *
 *  Compliance is drawn as columns, not lines. It is a PARTITION of each
 *  bucket: four shares that add to 100 and cannot cross, which is what a
 *  stacked column says and what four lines would deny. The figure riding
 *  each column is the in-band share, because that is the number people
 *  quote; the other three are in the tip. Hue is state - the same cool /
 *  ok / warn / critical the table's spread bar and the inlet alarm rules
 *  use - so a column and a table row cannot disagree about how bad an hour
 *  was.
 */

import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type ThermalComplianceTrend as ComplianceData,
         type ThermalTrend as TrendData } from '../../api/client';
import { Seg } from '../../components/estate';
import { useHoverTip } from '../../components/HoverTip';
import { MaxGlyph, MaxModal } from '../../components/MaxModal';
import { Plot, PLOT_COLORS, type PlotSeries } from '../../components/Plot';
import { LINE_COLORS } from '../../components/TimeChart';

export interface ThermalTrendScope { kind: 'site' | 'room' | 'rack'; id: string; label: string }

type Unit = 'c' | 'f';
const conv = (c: number | null, unit: Unit) =>
  c === null ? NaN : unit === 'c' ? c : c * 9 / 5 + 32;

/** How far back. Hourly points up to a week; a month or a quarter is a
 *  point per day, because 2,160 hourly points is a texture, not a line.
 *  CUSTOM is the fifth cell: two dates, honoured to the day, hourly when
 *  the span is a week or less. */
const RANGES = [
  { label: '24H', days: 1, bucket: 'hour', said: 'in the last 24 hours' },
  { label: '7D', days: 7, bucket: 'hour', said: 'in the last 7 days' },
  { label: '30D', days: 30, bucket: 'day', said: 'in the last 30 days' },
  { label: '90D', days: 90, bucket: 'day', said: 'in the last 90 days' },
] as const;
type Range = typeof RANGES[number];
const CUSTOM = 'CUSTOM';
const HOURLY_UP_TO_DAYS = 7;
const MAX_DAYS = 366;

const isoDay = (d: Date) => d.toISOString().slice(0, 10);
const daysBetween = (a: string, b: string) =>
  Math.round((Date.parse(b) - Date.parse(a)) / 86_400_000) + 1;
const shortDay = (iso: string) => iso.slice(5);
const pad2 = (n: number) => String(n).padStart(2, '0');
/** Axis labels: the day, and for hourly points the hour too. UTC, as the
 *  page's days are. */
function label(ms: number, bucket: 'hour' | 'day'): string {
  const d = new Date(ms);
  const day = `${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}`;
  return bucket === 'hour' ? `${day} ${pad2(d.getUTCHours())}:00` : day;
}

function TrendPlot({ data, unit }: { data: TrendData; unit: Unit }) {
  const u = unit === 'c' ? '°C' : '°F';
  // The SVG is drawn at the panel's pixel width, not scaled up from a
  // 720px viewBox: scaling scales the tick text with it, and a 10px label
  // at 2.3x reads as a heading. Measured, so the maximized copy and the
  // inline one each get their own width.
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(720);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? 0;
      if (w > 0) setWidth(Math.round(w));
    });
    ro.observe(el);
    const w = el.getBoundingClientRect().width;
    if (w > 0) setWidth(Math.round(w));
    return () => ro.disconnect();
  }, []);
  const bucket = data.bucket;
  const pts = data.points.map((p) => ({ ...p, x: Date.parse(p.t) }));
  const series: PlotSeries[] = [
    { label: 'Average', color: LINE_COLORS[0], points: pts.map((p) => [p.x, conv(p.avg_c, unit)]) },
    { label: 'p90', color: LINE_COLORS[1], points: pts.map((p) => [p.x, conv(p.p90_c, unit)]) },
    { label: 'Max', color: LINE_COLORS[2], points: pts.map((p) => [p.x, conv(p.max_c, unit)]) },
  ];
  const x0 = pts[0]?.x ?? 0;
  const x1 = pts[pts.length - 1]?.x ?? 0;
  const lo = conv(data.band.low_c, unit);
  const hi = conv(data.band.high_c, unit);
  const allow = conv(data.band.allowable_high_c, unit);
  return (
    <div className="trend-frame" ref={box}>
    <Plot
      series={series}
      width={width}
      band={{ label: `ASHRAE recommended ${lo.toFixed(0)}–${hi.toFixed(0)} ${u}`,
              color: PLOT_COLORS.ok, points: [[x0, lo, hi], [x1, lo, hi]] }}
      refs={[{ value: allow, label: `allowable ${allow.toFixed(0)} ${u}`, color: PLOT_COLORS.critical }]}
      unit={u}
      xFormat={(v) => label(v, bucket)}
      xTip={(v) => `${label(v, bucket)} UTC`}
      yFormat={(v) => v.toFixed(1)}
      height={220}
      breakGaps
      hover
      empty="No intake readings in this window."
    />
    </div>
  );
}

/** The four-way split per bucket, as stacked columns sized to their box.
 *
 *  Its own component because the same points are drawn twice when the panel
 *  is maximized, and each drawing measures its own width: 168 hourly columns
 *  across a full-width panel and across a modal are different charts, and
 *  what to hide is decided from pixels per column, not from the count. The
 *  alarm trend's rule, and its class names - one column chart in this
 *  product, whatever it counts.
 */
function ComplianceColumns({ data, said, band, unit }: {
  data: ComplianceData; said: string;
  band: { low_c: number; high_c: number; allowable_high_c: number };
  unit: Unit;
}) {
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

  const pts = data.points;
  // A percentage needs ~30px before it collides with its neighbour; a
  // timestamp label ~46px at day width and ~58px with an hour on it. Until
  // the box is measured, assume room - a first paint that hides everything
  // and then shows it flickers.
  const per = width > 0 ? width / Math.max(1, pts.length) : 999;
  const dense = per < 30;
  const every = Math.max(1, Math.ceil((data.bucket === 'hour' ? 58 : 46) / per));
  const deg = (c: number) => (unit === 'c' ? c : c * 9 / 5 + 32).toFixed(0);
  const u = unit === 'c' ? '°C' : '°F';
  const segs = (p: ComplianceData['points'][number]): Array<[string, number, string]> => [
    ['cool', p.below_pct ?? 0, `below ${deg(band.low_c)} ${u} (overcooled)`],
    ['ok', p.in_band_pct ?? 0, `${deg(band.low_c)}–${deg(band.high_c)} ${u} recommended`],
    ['warn', p.above_recommended_pct ?? 0,
      `${deg(band.high_c)}–${deg(band.allowable_high_c)} ${u} allowable`],
    ['critical', p.above_allowable_pct ?? 0, `above ${deg(band.allowable_high_c)} ${u}`],
  ];

  return (
    <div className="alarm-trend-frame" ref={box}>
      {/* The vertical dimension is the whole bucket, divided four ways -
          not the in-band part of it, which is only the green. */}
      <div className="alarm-trend-ylabel">
        Share of the {data.bucket === 'hour' ? 'hour' : 'day'}
      </div>
      <div className={`alarm-trend trend-partition ${dense ? 'dense' : ''}`} role="img"
           aria-label={`Share of each ${data.bucket} spent in each ASHRAE band, ${said}`}>
        {pts.map((p, i) => {
          const had = p.in_band_pct !== null;
          const parts = segs(p);
          return (
            <div className="col" key={p.t}
                 {...bind(<>
                   <span className="spread-line"><b>{label(Date.parse(p.t), data.bucket)} UTC</b></span>
                   {had ? parts.map(([k, v, what]) => (
                     <span key={k} className="spread-line"><b>{v.toFixed(1)}%</b> {what}</span>
                   )) : <span className="spread-line">nothing measured</span>}
                   {had && (
                     <span className="spread-line">{p.sensors} sensor{p.sensors === 1 ? '' : 's'}</span>
                   )}
                 </>)}>
              <div className="barwrap">
                <div className="v">{had ? `${Math.round(p.in_band_pct as number)}%` : ''}</div>
                {/* A bucket nothing reported in is left EMPTY rather than
                    drawn as 0% in band: no reading is not a cold hour, and
                    a full-height critical column would invent an excursion
                    nobody measured. */}
                {had && (
                  <div className="stackcol">
                    {parts.filter(([, v]) => v > 0).map(([k, v]) => (
                      <div key={k} className={`seg ${k}`} style={{ height: `${v}%` }} />
                    ))}
                  </div>
                )}
              </div>
              <div className={`k ${dense && i % every !== 0 ? 'hide' : ''}`}>
                {label(Date.parse(p.t), data.bucket)}
              </div>
            </div>
          );
        })}
        {tipEl}
      </div>
    </div>
  );
}

/** The window both panels are drawn on.
 *
 *  One piece of state, one control, two charts. Held above the panels rather
 *  than by either of them because a range control per panel lets them drift,
 *  and two charts of one scope over two different weeks is a worse page than
 *  either chart alone.
 *
 *  Returns what a panel needs to ask its question (`days`, `bucket`, the
 *  picked window) and the controls themselves, which the INTAKE panel
 *  renders and the compliance panel does not.
 */
export function useTrendRange() {
  const [range, setRange] = useState<Range>(RANGES[1]);
  const [custom, setCustom] = useState(false);
  const [since, setSince] = useState(() => {
    const d = new Date(); d.setDate(d.getDate() - 6); return isoDay(d);
  });
  const [until, setUntil] = useState(() => isoDay(new Date()));
  const span = daysBetween(since, until);
  const windowOk = span >= 1 && span <= MAX_DAYS;
  const bucket: 'hour' | 'day' = custom
    ? (span > HOURLY_UP_TO_DAYS ? 'day' : 'hour')
    : range.bucket;

  const picker = (
    <Seg value={custom ? CUSTOM : range.label} label="How far back"
         options={[...RANGES.map((r) => ({ key: r.label, label: r.label })),
                   { key: CUSTOM, label: CUSTOM }]}
         onChange={(k) => {
           if (k === CUSTOM) { setCustom(true); return; }
           setCustom(false);
           setRange(RANGES.find((r) => r.label === k) ?? RANGES[1]);
         }} />
  );
  const pick = (set: (v: string) => void) => (e: React.ChangeEvent<HTMLInputElement>) => {
    set(e.target.value);
    setCustom(true);
  };
  const dates = custom ? (
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
  ) : null;

  return {
    days: range.days,
    bucket,
    custom,
    windowOk,
    /** The picked window, or undefined on a preset. */
    window: custom ? { since, until } : undefined,
    /** For the caption: "in the last 7 days", or the two dates. */
    said: custom ? `from ${shortDay(since)} to ${shortDay(until)}` : range.said,
    /** A key that changes with the window, for the query caches. */
    key: custom ? `${since}..${until}` : String(range.days),
    picker,
    dates,
  };
}
export type TrendRange = ReturnType<typeof useTrendRange>;

/** What both panels share: the caption, whatever control the panel owns (the
 *  range Seg on the intake one, nothing on the other), the maximize glyph,
 *  and the same children drawn twice - inline and in the modal - so a
 *  maximized chart is never a stale copy. */
function TrendPanel({ title, caption, controls, dates, children }: {
  title: string;
  caption: React.ReactNode;
  controls?: React.ReactNode;
  dates?: React.ReactNode;
  children: React.ReactNode;
}) {
  const [maxed, setMaxed] = useState(false);
  const head = (inline: boolean) => (
    <>
      <div className="alarm-trend-head">
        <p className="muted">{caption}</p>
        {controls}
        {inline && (
          <button type="button" className="asset-max"
                  aria-label={`Maximize ${title}`} title="Maximize"
                  onClick={() => setMaxed(true)}><MaxGlyph /></button>
        )}
      </div>
      {dates}
    </>
  );
  return (
    <div className="trend-panel">
      <h3>{title}</h3>
      {head(true)}
      {children}
      {maxed && (
        <MaxModal title={title} onClose={() => setMaxed(false)}>
          {head(false)}
          {children}
        </MaxModal>
      )}
    </div>
  );
}

/** Where the chart is pointed, said the way a caption says it. */
const whereOf = (scope?: ThermalTrendScope) =>
  scope ? `in ${scope.label}` : 'across the estate';

export function ThermalTrend({ scope, unit, source = 'auto', range }: {
  scope?: ThermalTrendScope;
  unit: Unit;
  /** The intake pin the tables above are using. The chart follows it because
   *  it is drawn from the same readings: a page pinned to PROBES with a chart
   *  still drawn the automatic way shows two measurements of one estate and
   *  says nothing about the difference. */
  source?: string;
  /** The shared window. This panel carries its control. */
  range: TrendRange;
}) {
  const { data, error } = useQuery({
    queryKey: ['thermal-trend', scope?.kind ?? '', scope?.id ?? '',
               range.key, range.bucket, source],
    queryFn: () => api.thermalTrend({
      site: scope?.kind === 'site' ? scope.id : undefined,
      room: scope?.kind === 'room' ? scope.id : undefined,
      rack: scope?.kind === 'rack' ? scope.id : undefined,
    }, range.days, range.bucket, range.window, source),
    enabled: !range.custom || range.windowOk,
    // Hourly points move every five minutes; a wall display should follow.
    refetchInterval: range.bucket === 'hour' ? 300_000 : false,
    staleTime: 60_000,
    // Keep the old line up while the new range loads: a chart that blanks
    // on every click reads as broken, and the ranges are compared by eye.
    placeholderData: (prev) => prev,
  });

  const where = whereOf(scope);
  const per = range.bucket === 'hour' ? 'per hour' : 'per day';
  const caption = error ? 'Could not load the trend.'
    : range.custom && !range.windowOk ? 'Pick a window to draw.'
      : !data ? 'Loading the trend…'
        : data.buckets_with_data === 0
          ? <>No intake readings {where} {range.said}.</>
          : <>
              <b>Intake</b> {where} {range.said}, {per}, from{' '}
              <b>{data.sensors}</b> sensor{data.sensors === 1 ? '' : 's'} at most;
              {' '}p90 over {data.source === '5m' ? 'five-minute' : 'hourly'} sensor values
            </>;
  const drawable = !error && data && !(range.custom && !range.windowOk);

  return (
    <TrendPanel title={`Intake ${where}`} caption={caption}
                controls={range.picker} dates={range.dates}>
      {drawable && data ? <TrendPlot data={data} unit={unit} /> : null}
    </TrendPanel>
  );
}

/** The same window, read as time in band rather than as temperature.
 *
 *  No range control of its own: the panel above owns the window for both, so
 *  this one is always drawn over exactly the hours the line above it is
 *  drawn over. Its caption still names the window, because a maximized chart
 *  is read on its own and a chart that does not say what it covers says
 *  nothing.
 */
export function ThermalCompliance({ scope, unit, source = 'auto', range }: {
  scope?: ThermalTrendScope;
  unit: Unit;
  source?: string;
  range: TrendRange;
}) {
  const { data, error } = useQuery({
    queryKey: ['thermal-compliance-trend', scope?.kind ?? '', scope?.id ?? '',
               range.key, range.bucket, source],
    queryFn: () => api.thermalComplianceTrend({
      site: scope?.kind === 'site' ? scope.id : undefined,
      room: scope?.kind === 'room' ? scope.id : undefined,
      rack: scope?.kind === 'rack' ? scope.id : undefined,
    }, range.days, range.bucket, range.window, source),
    enabled: !range.custom || range.windowOk,
    refetchInterval: range.bucket === 'hour' ? 300_000 : false,
    staleTime: 60_000,
    placeholderData: (prev) => prev,
  });

  const where = whereOf(scope);
  const per = range.bucket === 'hour' ? 'per hour' : 'per day';
  const caption = error ? 'Could not load the trend.'
    : range.custom && !range.windowOk ? 'Pick a window to draw.'
      : !data ? 'Loading the trend…'
        : data.buckets_with_data === 0
          ? <>No intake readings {where} {range.said}.</>
          : <>
              <b>{data.in_band_pct}%</b> of the time in band {where} {range.said},
              {' '}<b>{data.below_pct}%</b> below it, {per}, from{' '}
              <b>{data.sensors}</b> sensor{data.sensors === 1 ? '' : 's'} at most;
              {' '}each sensor weighted once
            </>;
  const drawable = !error && data && !(range.custom && !range.windowOk);

  return (
    <TrendPanel title={`Time in band ${where}`} caption={caption}>
      {drawable && data
        ? <ComplianceColumns data={data} said={range.said} band={data.band} unit={unit} />
        : null}
    </TrendPanel>
  );
}
