import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type ForecastResult, type RoomSummary } from '../../api/client';
import { Plot, PLOT_COLORS, type PlotSeries } from '../../components/Plot';

/** The forecast, and - far more often - the refusal to make one.
 *
 *  Below fourteen days of history the backend returns no numbers at all. This
 *  view has to render that as a stated position rather than as an empty chart,
 *  because an empty chart reads as "nothing is happening" when the truth is
 *  "not enough has been recorded to say". The progress bar towards the
 *  threshold is there so the answer to "why is this blank" is on the screen.
 *
 *  When there IS a forecast, history and projection are drawn in one line with
 *  the projected half dashed, the prediction interval shaded behind it, and the
 *  capacity - if any is known - as a threshold line. The runway is given as a
 *  window, never as a single date.
 */

const METRICS = [
  { key: 'power', label: 'Total load' },
  { key: 'it_power', label: 'IT load only' },
];

function Refusal({ data }: { data: ForecastResult }) {
  const pct = Math.min(100, (data.history_days / data.min_history_days) * 100);
  return (
    <div className="refusal">
      <div className="refusal-head">No forecast — not enough history</div>
      <p>{data.method_reason}</p>
      <div className="meter-track">
        <div className="meter-fill warn" style={{ width: `${pct}%` }} />
      </div>
      <div className="meter-foot">
        <span className="muted">
          {data.history_days} of {data.min_history_days} qualifying days
        </span>
      </div>
      <p className="muted small">
        A day counts once it carries data in at least 20 of its 24 hours. Below
        the threshold nothing is shown on purpose: a trend fitted to nine days
        cannot tell growth from a quiet weekend, and it would still arrive with
        a date attached.
      </p>
    </div>
  );
}

export function ForecastView({ room }: { room: RoomSummary }) {
  const [metric, setMetric] = useState('power');
  const [capacity, setCapacity] = useState('');
  const cap = Number(capacity) > 0 ? Number(capacity) : undefined;

  const { data, error, isLoading } = useQuery<ForecastResult>({
    queryKey: ['forecast', room.id, metric, cap],
    queryFn: () => api.forecast('room', room.id, {
      metric, horizonDays: 30, capacity: cap,
    }),
    staleTime: 60_000,
  });

  if (isLoading) return <p className="muted">Loading…</p>;
  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (!data) return null;

  const historyPts: [number, number][] = data.history.map((h, i) => [
    i - data.history.length + 1, h.value,
  ]);
  const lastHistory = historyPts.at(-1);
  const projPts: [number, number][] = data.points.map((p) => [p.day, p.value]);
  const series: PlotSeries[] = [];
  if (historyPts.length) {
    series.push({ label: 'measured', points: historyPts, color: PLOT_COLORS.primary });
  }
  if (projPts.length) {
    series.push({
      label: `projected (${data.method === 'holt_winters' ? 'weekly seasonal' : 'linear'})`,
      // Joined to the last measured point so the projection starts where the
      // data ends rather than floating a day to the right of it.
      points: lastHistory ? [lastHistory, ...projPts] : projPts,
      color: PLOT_COLORS.projection,
      dashed: true,
    });
  }

  return (
    <>
      <div className="toolbar">
        <label htmlFor="fmetric">Metric</label>
        <select id="fmetric" value={metric} onChange={(e) => setMetric(e.target.value)}>
          {METRICS.map((m) => <option key={m.key} value={m.key}>{m.label}</option>)}
        </select>
        <label htmlFor="fcap">Capacity (kW)</label>
        <input id="fcap" value={capacity} placeholder="none recorded"
               onChange={(e) => setCapacity(e.target.value)} style={{ width: 120 }} />
        <span className="muted small">
          no rack or PDU in this fleet carries a rating, so a runway needs one here
        </span>
      </div>

      <p className="muted small">
        {data.metric_label} · {data.statistic} · {data.devices} devices
      </p>

      {data.method === 'insufficient_history' ? (
        <Refusal data={data} />
      ) : (
        <>
          <div className="verdict">
            <div className="verdict-label">Runway</div>
            <div className="verdict-value">
              {data.runway.days === null ? 'no crossing in the horizon'
                : `about ${data.runway.days} days`}
            </div>
            <p className="verdict-reason">{data.runway.reason}</p>
          </div>

          <Plot series={series}
                band={data.points.length ? {
                  label: '95% interval',
                  points: data.points.map((p) => [p.day, p.lower, p.upper]),
                } : undefined}
                refs={data.capacity ? [{ value: data.capacity, label: 'capacity' }] : []}
                unit={data.unit}
                xFormat={(v) => (v <= 0 ? `${Math.round(v)}d` : `+${Math.round(v)}d`)} />

          <div className="stat-row">
            <div><span className="muted">Method</span> {data.method}</div>
            <div><span className="muted">Trend</span> {data.trend_per_day} {data.unit}/day</div>
            <div><span className="muted">R²</span> {data.r2 ?? '—'}</div>
            <div><span className="muted">History</span> {data.history_days} days</div>
          </div>
          <p className="muted small">{data.method_reason}</p>
        </>
      )}

      {data.notes.map((n) => <p key={n} className="muted small">{n}</p>)}
    </>
  );
}
