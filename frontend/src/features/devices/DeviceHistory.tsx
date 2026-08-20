import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { api, type HistoryOut, type Series } from '../../api/client';
import { TimeChart } from '../../components/TimeChart';

const RANGES = [
  { key: '1h', label: '1 h', ms: 3600_000 },
  { key: '6h', label: '6 h', ms: 6 * 3600_000 },
  { key: '24h', label: '24 h', ms: 24 * 3600_000 },
  { key: '7d', label: '7 d', ms: 7 * 86400_000 },
  { key: '30d', label: '30 d', ms: 30 * 86400_000 },
];

const BUCKET_MS: Record<string, number> = {
  raw: 60_000, '1m': 60_000, '5m': 300_000, '1h': 3600_000,
};

/** Charts are grouped by unit, never merged across units.
 *  Degrees and watts on one axis produce a picture where neither line means
 *  anything. */
function groupByUnit(series: Series[]): Map<string, Series[]> {
  const out = new Map<string, Series[]>();
  for (const s of series) {
    (out.get(s.unit) ?? out.set(s.unit, []).get(s.unit)!).push(s);
  }
  return out;
}

export function DeviceHistory({ deviceId, metrics }: {
  deviceId: string;
  metrics: string[];
}) {
  const [range, setRange] = useState('6h');
  const span = RANGES.find((r) => r.key === range)?.ms ?? 6 * 3600_000;

  // Anchored to a value that only changes when the range does, so a re-render
  // does not shift the window and refetch on every paint.
  const { startIso, endIso } = useMemo(() => {
    const end = Date.now();
    return {
      startIso: new Date(end - span).toISOString(),
      endIso: new Date(end).toISOString(),
    };
  }, [span]);

  const q = useQuery<HistoryOut>({
    queryKey: ['history', deviceId, range, metrics.join(',')],
    queryFn: () => api.history(deviceId, metrics, startIso, endIso),
    enabled: Boolean(deviceId) && metrics.length > 0,
    retry: false,
  });

  if (!metrics.length) {
    return <p className="muted">This device is not reporting any metrics yet.</p>;
  }

  const groups = q.data ? groupByUnit(q.data.series.filter((s) => s.points.length)) : null;

  return (
    <section>
      <h3>History</h3>
      <div className="overlay-picker" role="group" aria-label="Range">
        {RANGES.map((r) => (
          <button key={r.key} type="button"
                  className={range === r.key ? 'active' : undefined}
                  onClick={() => setRange(r.key)}>
            {r.label}
          </button>
        ))}
      </div>

      {q.isLoading && <p className="muted">Loading…</p>}
      {q.isError && <p className="warn">Could not load history.</p>}

      {q.data && (
        <>
          <p className="muted">
            {q.data.interval === 'raw'
              ? 'Raw samples as polled.'
              : `Averaged into ${q.data.interval} buckets (${q.data.source}). `}
            {q.data.interval !== 'raw' &&
              'The bucket is chosen to keep the chart to a readable number of points, so a longer range is a coarser average — not more detail.'}
          </p>
          {groups && groups.size === 0 && (
            <p className="muted">Nothing recorded in this window.</p>
          )}
          {groups && [...groups.entries()].map(([unit, series]) => (
            <figure key={unit} className="chart-figure">
              <figcaption>
                {[...new Set(series.map((s) => s.metric))].join(', ')}
                <span className="muted"> · {unit}</span>
              </figcaption>
              <TimeChart series={series} unit={unit}
                         bucketMs={BUCKET_MS[q.data.interval] ?? 60_000} />
              {series.length > 1 && (
                <figcaption className="muted">
                  {series.length} series — one per instance
                </figcaption>
              )}
            </figure>
          ))}
        </>
      )}
    </section>
  );
}
