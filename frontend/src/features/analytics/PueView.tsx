import { useQuery } from '@tanstack/react-query';
import { api, type PueResult, type PueSeries, type RoomSummary } from '../../api/client';
import { Plot, PLOT_COLORS } from '../../components/Plot';

/** PUE, never as a bare number.
 *
 *  The measurement level changes the value: IT energy taken at the UPS output
 *  includes distribution losses that IT energy taken at the equipment inlet
 *  does not, so the same site reports a lower PUE at Category 3 than at
 *  Category 1. A PUE without its category cannot be compared with anyone
 *  else's, so the category and the measurement point are shown next to the
 *  figure rather than in a tooltip.
 *
 *  The 1.0 line is drawn on the trend because a value below it is not a very
 *  efficient datacenter, it is a double-counted meter.
 */

export function PueView({ room }: { room: RoomSummary }) {
  const dc = room.datacenter_id ?? undefined;

  const { data, error, isLoading } = useQuery<PueResult>({
    queryKey: ['pue', dc],
    queryFn: () => api.pue(dc ? { datacenter_id: dc } : {}),
    refetchInterval: 60_000,
  });
  const { data: series } = useQuery<PueSeries>({
    queryKey: ['pue-series', dc],
    queryFn: () => api.pueSeries(dc ? { datacenter_id: dc } : {}),
    staleTime: 60_000,
  });

  if (isLoading) return <p className="muted">Loading…</p>;
  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (!data) return null;

  const pts: [number, number][] = (series?.points ?? [])
    .filter((p) => p.pue !== null)
    .map((p) => [new Date(p.end).getTime(), p.pue as number]);

  // Tick labels sized to the window. A day of buckets wants clock readings; a
  // month wants dates. Fixed to the date, a short window prints the same label
  // three times and the axis says nothing at all.
  const span = pts.length ? pts[pts.length - 1][0] - pts[0][0] : 0;
  const xFormat = (v: number) => {
    const d = new Date(v);
    const p2 = (n: number) => String(n).padStart(2, '0');
    return span <= 36 * 3600_000
      ? `${p2(d.getHours())}:${p2(d.getMinutes())}`
      : `${d.getMonth() + 1}/${p2(d.getDate())}`;
  };

  return (
    <>
      <div className="tiles">
        <div className="tile">
          <div className="label">PUE</div>
          <div className="value">{data.pue?.toFixed(3) ?? '—'}</div>
          <div className="detail">
            {data.category ? `Green Grid Category ${data.category}` : 'no category'}
          </div>
        </div>
        <div className="tile">
          <div className="label">Method</div>
          <div className="value" style={{ fontSize: 20 }}>{data.method ?? '—'}</div>
          <div className="detail">
            {data.method === 'energy'
              ? 'kWh counters over the window — the Green Grid definition'
              : data.method === 'power'
                ? 'instantaneous ratio, not an energy PUE'
                : 'not computable'}
          </div>
        </div>
        <div className="tile">
          <div className="label">Measured at</div>
          <div className="value" style={{ fontSize: 15 }}>
            {data.measurement_point ?? '—'}
          </div>
          <div className="detail">
            {data.meters ? `${data.meters.facility} facility · ${data.meters.it} IT meters` : ''}
          </div>
        </div>
        <div className="tile">
          <div className="label">Energy in window</div>
          <div className="value" style={{ fontSize: 20 }}>
            {data.total_facility_kwh !== undefined
              ? `${data.total_facility_kwh} / ${data.it_kwh} kWh`
              : data.total_facility_kw !== undefined
                ? `${data.total_facility_kw} / ${data.it_kw} kW`
                : '—'}
          </div>
          <div className="detail">facility / IT</div>
        </div>
      </div>

      {!data.plausible && (
        <div className="banner">
          {data.note ?? 'This PUE is not plausible.'}
        </div>
      )}
      {data.plausible && data.note && <p className="muted small">{data.note}</p>}
      {(data.counter_resets ?? 0) > 0 && (
        <p className="muted small">
          {data.counter_resets} counter reset(s) in the window — energy is summed
          from positive increments, so a meter that rolled over or was replaced
          does not subtract from the total.
        </p>
      )}

      <h3>Trend</h3>
      {/* A lone point on a wide axis implies a continuous measurement with one
          sample. Saying how many buckets had nothing is the difference between
          "PUE was steady" and "PUE was measurable once". */}
      {series && series.buckets > pts.length && (
        <p className="muted small">
          {series.buckets - pts.length} of {series.buckets} buckets had no
          usable meter reading and are not plotted.
        </p>
      )}
      {pts.length ? (
        <Plot series={[{ label: 'PUE', points: pts, color: PLOT_COLORS.primary }]}
              refs={[{ value: 1.0, label: '1.0 — impossible below', color: PLOT_COLORS.critical }]}
              unit="ratio"
              xFormat={xFormat} />
      ) : (
        <p className="muted">
          No bucket in the window had usable energy on both meters. PUE is an
          energy ratio over a period; too short a window and the counters have
          not incremented.
        </p>
      )}
    </>
  );
}
