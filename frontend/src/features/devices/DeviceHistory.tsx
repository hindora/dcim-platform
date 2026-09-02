import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { api, type HistoryOut, type Series } from '../../api/client';
import { metricLabel } from '../../lib/format';
import { TimeChart } from '../../components/TimeChart';

const RANGES = [
  { key: '1h', label: '1 h', ms: 3600_000 },
  { key: '6h', label: '6 h', ms: 6 * 3600_000 },
  { key: '24h', label: '24 h', ms: 24 * 3600_000 },
  { key: '7d', label: '7 d', ms: 7 * 86400_000 },
  { key: '30d', label: '30 d', ms: 30 * 86400_000 },
];

const BUCKET_MS: Record<string, number> = {
  raw: 60_000, '1m': 60_000, '5m': 300_000, '1h': 3600_000, '1d': 86400_000,
};

/** Charts are grouped by unit, never merged across units.
 *  Degrees and watts on one axis produce a picture where neither line means
 *  anything. */
/** How far the chart reaches, at the precision the bucket justifies.
 *
 *  A daily bucket has no meaningful clock reading - "Sep 2, 00:00" would claim
 *  a midnight measurement for a whole day's average - and a clock alone is
 *  ambiguous once the data is older than today.
 */
function stampFor(ms: number, interval?: string): string {
  const d = new Date(ms);
  const date = d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  if (interval === '1d') return date;
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const today = new Date().toDateString() === d.toDateString();
  return today ? time : `${date}, ${time}`;
}

function groupByUnit(series: Series[]): Map<string, Series[]> {
  const out = new Map<string, Series[]>();
  for (const s of series) {
    (out.get(s.unit) ?? out.set(s.unit, []).get(s.unit)!).push(s);
  }
  return out;
}

/** A named panel: metrics that belong on one frame because they share a unit
 *  AND a question. Grouping by unit alone is the fallback for device types
 *  nobody has curated yet - it will happily put a fan tachometer beside a CPU
 *  load because both happen to be percentages. */
/** Deliberately title + metrics only. There was a `caption` here for a note
 *  above the chart, and what it produced was prose explaining how to read two
 *  labelled lines. A panel that needs a sentence to be legible is a panel that
 *  needs different metrics or a better title. */
export type ChartGroup = { title: string; metrics: string[] };

export function DeviceHistory({ deviceId, metrics, groups }: {
  deviceId: string;
  metrics: string[];
  /** Curated panels. Omitted, the charts fall back to one per unit. */
  groups?: ChartGroup[];
}) {
  const [range, setRange] = useState('6h');
  const span = RANGES.find((r) => r.key === range)?.ms ?? 6 * 3600_000;

  /** The instant the window ends. Bumping it is what "refresh" means here.
   *
   *  These charts do NOT advance on their own, deliberately. The live question
   *  - is this device hot right now - is answered by the current-value table
   *  and the alarm; a trend chart answers what has happened since, and the
   *  moment it is worth reading is the moment somebody is hovering a point.
   *  Re-rendering the line under their cursor while they do that is hostile,
   *  and the underlying buckets are a minute wide anyway, so an auto-advancing
   *  chart would crawl rather than live.
   *
   *  Held in state rather than computed per render for the original reason: a
   *  window that moved on every paint would refetch on every paint.
   */
  const [endsAt, setEndsAt] = useState(() => Date.now());
  const { startIso, endIso } = useMemo(() => ({
    startIso: new Date(endsAt - span).toISOString(),
    endIso: new Date(endsAt).toISOString(),
  }), [span, endsAt]);

  const q = useQuery<HistoryOut>({
    queryKey: ['history', deviceId, range, metrics.join(','), endsAt],
    queryFn: () => api.history(deviceId, metrics, startIso, endIso),
    enabled: Boolean(deviceId) && metrics.length > 0,
    retry: false,
  });

  if (!metrics.length) {
    return <p className="muted">This device is not reporting any metrics yet.</p>;
  }

  const withData = q.data ? q.data.series.filter((s) => s.points.length) : [];
  // The newest instant any series reaches - what the chart actually shows.
  const lastPointAt = withData.length
    ? Math.max(...withData.map((s) => s.points[s.points.length - 1][0]))
    : null;
  const byUnit = q.data ? groupByUnit(withData) : null;

  // A curated panel keeps its DECLARED order - CPU, then memory, then disk -
  // rather than whatever order the series came back in, so the legend reads the
  // same on every device and after every reload. Colour follows that order, so
  // a metric that stops reporting must not repaint the ones that remain.
  const panels = groups?.map((g) => ({
    ...g,
    series: g.metrics.flatMap((k) => withData.filter((s) => s.metric === k)),
    missing: g.metrics.filter((k) => !withData.some((s) => s.metric === k)),
  }));

  return (
    <section>
      <h3>History</h3>
      <div className="overlay-picker" role="group" aria-label="Range">
        {/* Changing the range re-anchors to now as well: picking "24 h" means
            the last 24 hours from now, not 24 hours ending whenever the page
            happened to open. */}
        {RANGES.map((r) => (
          <button key={r.key} type="button"
                  className={range === r.key ? 'active' : undefined}
                  onClick={() => { setRange(r.key); setEndsAt(Date.now()); }}>
            {r.label}
          </button>
        ))}
        {/* Not one of the range options, so not inside the group: it would read
            as an eighth range. Explicit refresh instead of a timer - the reader
            decides when the ground moves under them. */}
        <button type="button" className="refresh"
                onClick={() => setEndsAt(Date.now())}
                disabled={q.isFetching}
                title="Re-read up to now">
          {q.isFetching ? 'Loading…' : 'Refresh'}
        </button>
        {/* Sits with the control that changes it, not in a sentence above the
            charts. Two facts an operator needs and nothing else: how far the
            line reaches, and a way to move it. The bucket width is a caveat on
            reading a peak - hourly averaging flattens one - so it stays
            available on hover rather than printed at everybody all the time. */}
        {lastPointAt != null && (
          <span className="as-of"
                title={q.data && q.data.interval !== 'raw'
                  ? `Averaged into ${q.data.interval} buckets, so a short spike may be flattened.`
                  : 'Raw samples as polled.'}>
            to{' '}
            <time dateTime={new Date(lastPointAt).toISOString()}>
              {stampFor(lastPointAt, q.data?.interval)}
            </time>
          </span>
        )}
      </div>

      {q.isLoading && <p className="muted">Loading…</p>}
      {q.isError && <p className="warn">Could not load history.</p>}

      {q.data && (
        <>
          {!panels && byUnit && byUnit.size === 0 && (
            <p className="muted">Nothing recorded in this window.</p>
          )}

          {/* Curated panels: each one answers a question, and the metrics on it
              share a unit so the frame has a single axis. */}
          {panels?.map((p) => (
            <figure key={p.title} className="chart-figure">
              <figcaption>{p.title}</figcaption>
              {p.series.length === 0 ? (
                <p className="muted">
                  Not reported by this device{p.missing.length
                    ? ` (${p.missing.map(metricLabel).join(', ')})` : ''}.
                </p>
              ) : (
                <>
                  <TimeChart series={p.series} unit={p.series[0].unit}
                             bucketMs={BUCKET_MS[q.data.interval] ?? 60_000} />
                  {/* Say what is absent rather than drawing a panel that looks
                      complete. A missing line and a flat one look identical. */}
                  {p.missing.length > 0 && (
                    <figcaption className="muted">
                      No data for {p.missing.map(metricLabel).join(', ')} in this window.
                    </figcaption>
                  )}
                </>
              )}
            </figure>
          ))}

          {!panels && byUnit && [...byUnit.entries()].map(([unit, series]) => (
            <figure key={unit} className="chart-figure">
              <figcaption>
                {[...new Set(series.map((s) => s.metric))].join(', ')}
                <span className="muted"> · {unit}</span>
              </figcaption>
              <TimeChart series={series} unit={unit}
                         bucketMs={BUCKET_MS[q.data.interval] ?? 60_000} />
            </figure>
          ))}
        </>
      )}
    </section>
  );
}
