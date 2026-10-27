/** Draw over time for the power page's current scope.
 *
 *  The table's Δ column compares two windows and nothing more. These are
 *  the run-up, drawn from the same readings the table sums - rack PDUs left
 *  out, every total coincident - over the one window the Thermal trends
 *  share: `useTrendRange` holds it and each panel wears the control.
 *
 *  Three panels, three questions, each on its own axis because they are
 *  three units:
 *
 *  DRAW - total, IT and cooling in kW as lines. The window's coincident
 *  peak is marked as a reference level and named in the caption, so "what
 *  did we peak at" is read off the chart rather than remembered from the
 *  table's PEAK cell. Other is drawn only where it is ever non-zero; this
 *  estate meters nothing in it, and a flat line at zero is a fourth series
 *  that says nothing.
 *
 *  PUE - the ratio of those two lines, against the line the header judges
 *  it by. A ratio of draw, not the energy-based figure under Analytics;
 *  the caption says so.
 *
 *  LOAD AGAINST RATING - IT as a share of what the scope is rated for, as
 *  columns in the alarm trend's form, each coloured by the band the
 *  table's rows use. Hue is state: an amber hour on the chart and an
 *  amber row in the table are the same judgement. Where nothing is rated
 *  the panel stays and says so, rather than disappearing.
 */

import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type PowerTrend as TrendData } from '../../api/client';
import { useHoverTip } from '../../components/HoverTip';
import { Plot, type PlotSeries } from '../../components/Plot';
import { DASHES, useChartColors } from '../../components/seriesColors';
import { toCss } from '../../lib/zoom';
import { label, TrendPanel, type TrendRange } from './ThermalTrend';

export interface PowerTrendScope { kind: 'site' | 'room' | 'rack'; id: string; label: string }

const whereOf = (scope?: PowerTrendScope) =>
  scope ? `in ${scope.label}` : 'across the estate';

function usePowerTrend(scope: PowerTrendScope | undefined, range: TrendRange) {
  return useQuery({
    queryKey: ['power-trend', scope?.kind ?? '', scope?.id ?? '', range.key, range.bucket],
    queryFn: () => api.powerTrend({
      site: scope?.kind === 'site' ? scope.id : undefined,
      room: scope?.kind === 'room' ? scope.id : undefined,
      rack: scope?.kind === 'rack' ? scope.id : undefined,
    }, range.days, range.bucket, range.window),
    enabled: !range.custom || range.windowOk,
    // Hourly points move every five minutes; a wall display should follow.
    refetchInterval: range.bucket === 'hour' ? 300_000 : false,
    staleTime: 60_000,
    // Keep the old line up while the new range loads.
    placeholderData: (prev) => prev,
  });
}

/** The panel's pixel width, measured, so the maximized copy and the inline
 *  one each draw at their own size rather than scaling tick text. */
function useWidth() {
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
    const w = toCss(el.getBoundingClientRect().width);
    if (w > 0) setWidth(Math.round(w));
    return () => ro.disconnect();
  }, []);
  return { box, width };
}

const nn = (v: number | null) => (v === null ? NaN : v);

function DrawPlot({ data }: { data: TrendData }) {
  const C = useChartColors();
  const { box, width } = useWidth();
  const pts = data.points.map((p) => ({ ...p, x: Date.parse(p.t) }));
  const hasOther = pts.some((p) => (p.other_kw ?? 0) > 0);
  const series: PlotSeries[] = [
    { label: 'Total', color: C.series[0], dash: DASHES[0],
      points: pts.map((p) => [p.x, nn(p.total_kw)]) },
    { label: 'IT', color: C.series[1], dash: DASHES[1],
      points: pts.map((p) => [p.x, nn(p.it_kw)]) },
    { label: 'Cooling', color: C.series[2], dash: DASHES[2],
      points: pts.map((p) => [p.x, nn(p.cooling_kw)]) },
    ...(hasOther ? [{ label: 'Other', color: C.series[3], dash: DASHES[3],
      points: pts.map((p) => [p.x, nn(p.other_kw)] as [number, number]) }] : []),
  ];
  const refs = data.peak
    ? [{ value: data.peak.total_kw,
         label: `peak ${data.peak.total_kw.toFixed(1)} kW ${label(Date.parse(data.peak.t), data.bucket)}`,
         color: C.warn }]
    : [];
  return (
    <div className="trend-frame" ref={box}>
      <Plot
        series={series}
        width={width}
        refs={refs}
        unit="kW"
        xFormat={(v) => label(v, data.bucket)}
        xTip={(v) => `${label(v, data.bucket)} UTC`}
        yFormat={(v) => v.toFixed(1)}
        height={220}
        breakGaps
        hover
        empty="No power readings in this window."
      />
    </div>
  );
}

function PuePlot({ data }: { data: TrendData }) {
  const C = useChartColors();
  const { box, width } = useWidth();
  const pts = data.points.map((p) => ({ ...p, x: Date.parse(p.t) }));
  const series: PlotSeries[] = [
    { label: 'PUE', color: C.series[0], dash: DASHES[0],
      points: pts.map((p) => [p.x, nn(p.pue)]) },
  ];
  return (
    <div className="trend-frame" ref={box}>
      <Plot
        series={series}
        width={width}
        refs={[{ value: data.bands.pue_warn, label: `${data.bands.pue_warn.toFixed(1)}`,
                 color: C.warn }]}
        unit=""
        xFormat={(v) => label(v, data.bucket)}
        xTip={(v) => `${label(v, data.bucket)} UTC`}
        yFormat={(v) => v.toFixed(2)}
        height={180}
        breakGaps
        hover
        empty="No IT draw in this window to divide by."
      />
    </div>
  );
}

/** IT as a share of the rating, per bucket, as columns coloured by band.
 *
 *  The alarm trend's form and class names: one column chart in this
 *  product, whatever it counts. The scale is 100 % of rating or the highest
 *  column if that is more, so a scope past its rating draws past the top
 *  rather than clipping. The peak bucket keeps its figure even when the
 *  columns are too dense to label, because it is the one somebody asks for.
 */
function LoadColumns({ data, said }: { data: TrendData; said: string }) {
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
    setWidth(toCss(el.getBoundingClientRect().width));
    return () => ro.disconnect();
  }, []);
  const pts = data.points;
  const per = width > 0 ? width / Math.max(1, pts.length) : 999;
  const dense = per < 30;
  const every = Math.max(1, Math.ceil((data.bucket === 'hour' ? 58 : 46) / per));
  const top = Math.max(100, ...pts.map((p) => p.load_pct ?? 0));
  const { warn_pct: warn, critical_pct: crit } = data.bands;
  const band = (v: number) => (v >= crit ? 'critical' : v >= warn ? 'warn' : 'ok');
  const peakT = data.peak?.t;
  return (
    <div className="alarm-trend-frame" ref={box}>
      <div className="alarm-trend-ylabel">IT, share of rating</div>
      <div className={`alarm-trend pw-load ${dense ? 'dense' : ''}`} role="img"
           aria-label={`IT draw as a share of the rating per ${data.bucket}, ${said}`}>
        {pts.map((p, i) => {
          const v = p.load_pct;
          const isPeak = p.t === peakT;
          return (
            <div className="col" key={p.t}
                 {...bind(<>
                   <span className="spread-line"><b>{label(Date.parse(p.t), data.bucket)} UTC</b></span>
                   {v === null
                     ? <span className="spread-line">nothing measured</span>
                     : <>
                         <span className="spread-line"><b>{v.toFixed(1)} %</b> of {data.rated_kw?.toFixed(1)} kW</span>
                         <span className="spread-line">{p.it_kw?.toFixed(1)} kW IT from {p.devices} device{p.devices === 1 ? '' : 's'}</span>
                         {isPeak && <span className="spread-line">the window's peak</span>}
                       </>}
                 </>)}>
              <div className="barwrap">
                <div className={`v ${isPeak ? 'keep' : ''}`}>{v === null ? '' : `${Math.round(v)}%`}</div>
                {v !== null && (
                  <div className={`bar ${band(v)} ${isPeak ? 'peak' : ''}`}
                       style={{ height: `${(100 * v) / top}%` }} />
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

function basisWords(basis: TrendData['rating_basis']): string {
  switch (basis) {
    case 'rack': return 'its own rating';
    case 'design': return 'the design IT figure';
    case 'racks': return 'the sum of its rack ratings';
    case 'rooms': return 'the sum of the rooms\' ratings';
    default: return 'no rating';
  }
}

export function PowerDraw({ scope, range }: { scope?: PowerTrendScope; range: TrendRange }) {
  const { data, error } = usePowerTrend(scope, range);
  const where = whereOf(scope);
  const per = range.bucket === 'hour' ? 'per hour' : 'per day';
  const caption = error ? 'Could not load the trend.'
    : range.custom && !range.windowOk ? 'Pick a window to draw.'
      : !data ? 'Loading the trend…'
        : data.buckets_with_data === 0
          ? <>No power readings {where} {range.said}.</>
          : <>
              <b>Draw</b> {where} {range.said}, {per}, from <b>{data.devices}</b> device
              {data.devices === 1 ? '' : 's'} at most
              {data.peak && <>; peaked at <b>{data.peak.total_kw.toFixed(1)} kW</b> on{' '}
                {label(Date.parse(data.peak.t), data.bucket)} UTC</>}
            </>;
  const drawable = !error && data && !(range.custom && !range.windowOk);
  return (
    <TrendPanel title={`Draw ${where}`} caption={caption}
                controls={range.picker} dates={range.dates}>
      {drawable && data ? <DrawPlot data={data} /> : null}
    </TrendPanel>
  );
}

export function PowerPue({ scope, range }: { scope?: PowerTrendScope; range: TrendRange }) {
  const { data, error } = usePowerTrend(scope, range);
  const where = whereOf(scope);
  const per = range.bucket === 'hour' ? 'per hour' : 'per day';
  const withPue = data?.points.filter((p) => p.pue !== null) ?? [];
  const mean = withPue.length
    ? withPue.reduce((a, p) => a + (p.pue as number), 0) / withPue.length : null;
  const caption = error ? 'Could not load the trend.'
    : range.custom && !range.windowOk ? 'Pick a window to draw.'
      : !data ? 'Loading the trend…'
        : withPue.length === 0
          ? <>No IT draw {where} {range.said} to divide by.</>
          : <>
              <b>PUE {mean?.toFixed(3)}</b> on average {where} {range.said}, {per}: total over IT
              draw, not the energy-based figure under Analytics
            </>;
  const drawable = !error && data && !(range.custom && !range.windowOk);
  return (
    <TrendPanel title={`PUE ${where}`} caption={caption}
                controls={range.picker} dates={range.dates}>
      {drawable && data ? <PuePlot data={data} /> : null}
    </TrendPanel>
  );
}

export function PowerLoad({ scope, range }: { scope?: PowerTrendScope; range: TrendRange }) {
  const { data, error } = usePowerTrend(scope, range);
  const where = whereOf(scope);
  const per = range.bucket === 'hour' ? 'per hour' : 'per day';
  const rated = Boolean(data?.rated_kw);
  const caption = error ? 'Could not load the trend.'
    : range.custom && !range.windowOk ? 'Pick a window to draw.'
      : !data ? 'Loading the trend…'
        : !rated
          ? <>Nothing {where} carries a rating, so there is no share to draw. A rack's
              rating is its own; a room's is its design IT figure or the sum of its
              racks'.</>
          : data.buckets_with_data === 0
            ? <>No power readings {where} {range.said}.</>
            : <>
                <b>IT against {data.rated_kw?.toFixed(1)} kW</b> ({basisWords(data.rating_basis)}){' '}
                {where} {range.said}, {per}
                {data.peak?.load_pct !== null && data.peak?.load_pct !== undefined
                  && <>; highest <b>{data.peak.load_pct.toFixed(1)} %</b></>}
              </>;
  const drawable = !error && data && rated && !(range.custom && !range.windowOk);
  return (
    <TrendPanel title={`Load against rating ${where}`} caption={caption}
                controls={range.picker} dates={range.dates}>
      {drawable && data ? <LoadColumns data={data} said={range.said} /> : null}
    </TrendPanel>
  );
}
