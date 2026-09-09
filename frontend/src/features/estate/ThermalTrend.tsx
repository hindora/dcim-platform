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
 */

import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type ThermalTrend as TrendData } from '../../api/client';
import { Seg } from '../../components/estate';
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

export function ThermalTrend({ scope, unit }: { scope?: ThermalTrendScope; unit: Unit }) {
  const [range, setRange] = useState<Range>(RANGES[1]);
  const [maxed, setMaxed] = useState(false);
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

  const { data, error } = useQuery({
    queryKey: ['thermal-trend', scope?.kind ?? '', scope?.id ?? '',
               custom ? `${since}..${until}` : range.days, bucket],
    queryFn: () => api.thermalTrend({
      site: scope?.kind === 'site' ? scope.id : undefined,
      room: scope?.kind === 'room' ? scope.id : undefined,
      rack: scope?.kind === 'rack' ? scope.id : undefined,
    }, range.days, bucket, custom ? { since, until } : undefined),
    enabled: !custom || windowOk,
    // Hourly points move every five minutes; a wall display should follow.
    refetchInterval: bucket === 'hour' ? 300_000 : false,
    staleTime: 60_000,
    // Keep the old line up while the new range loads: a chart that blanks
    // on every click reads as broken, and the ranges are compared by eye.
    placeholderData: (prev) => prev,
  });

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

  const where = scope ? `in ${scope.label}` : 'across the estate';
  const title = `Intake ${where}`;
  const said = custom
    ? `from ${shortDay(since)} to ${shortDay(until)}`
    : range.said;
  const per = bucket === 'hour' ? 'per hour' : 'per day';
  const caption = error ? 'Could not load the trend.'
    : custom && !windowOk ? 'Pick a window to draw.'
      : !data ? 'Loading the trend…'
        : data.buckets_with_data === 0
          ? <>No intake readings {where} {said}.</>
          : <>
              <b>Intake</b> {where} {said}, {per}, from{' '}
              <b>{data.sensors}</b> sensor{data.sensors === 1 ? '' : 's'} at most;
              {' '}p90 over {data.source === '5m' ? 'five-minute' : 'hourly'} sensor values
            </>;
  const drawable = !error && data && !(custom && !windowOk);

  const head = (inline: boolean) => (
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

  return (
    <div className="trend-panel">
      <h3>{title}</h3>
      {head(true)}
      {drawable && <TrendPlot data={data} unit={unit} />}
      {maxed && (
        <MaxModal title={title} onClose={() => setMaxed(false)}>
          {head(false)}
          {drawable && <TrendPlot data={data} unit={unit} />}
        </MaxModal>
      )}
    </div>
  );
}
