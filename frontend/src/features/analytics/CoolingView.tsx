import { useQuery } from '@tanstack/react-query';
import { api, type CoolingPlant, type RoomSummary } from '../../api/client';
import { Meter } from '../../components/Meter';

/** Chiller staging, loop ΔT, and whether the instruments agree with each other.
 *
 *  The data-quality block is not a debug panel. Chiller output can be worked
 *  out two independent ways - from flow and ΔT across the loop, and from COP
 *  times input power - and when those two disagree by more than about 15% one
 *  of the instruments is wrong. A plant view that silently averages them
 *  reports a number that matches neither, so the disagreement is shown.
 */

const VERDICT_TONE: Record<string, string> = {
  ok: 'ok',
  n_plus_1: 'ok',
  tight: 'warn',
  low_delta_t: 'warn',
  disagreement: 'warn',
  no_capacity: 'critical',
  insufficient: 'critical',
};

function tone(v: string): string {
  return VERDICT_TONE[v] ?? 'unknown';
}

export function CoolingView({ room }: { room: RoomSummary }) {
  const dc = room.datacenter_id ?? undefined;
  const { data, error, isLoading } = useQuery<CoolingPlant>({
    queryKey: ['cooling', dc],
    queryFn: () => api.cooling(dc),
    refetchInterval: 60_000,
  });

  if (isLoading) return <p className="muted">Loading…</p>;
  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (!data) return null;

  return (
    <>
      <div className="verdict">
        <div className="verdict-label">Staging</div>
        <div className={`verdict-value ${tone(data.staging)}`}>
          {data.staging.replace(/_/g, ' ')}
        </div>
        <p className="verdict-reason">{data.reason}</p>
      </div>

      <div className="meters">
        <Meter label="plant load against running capacity"
               used={data.load_kw}
               capacity={data.running_capacity_kw || null}
               unit="kW"
               note={`${data.running} running · ${data.standby} standby` +
                 (data.nameplate_unknown
                   ? ` · ${data.nameplate_unknown} with no nameplate rating`
                   : '')} />
        <Meter label="plant load against installed capacity"
               used={data.load_kw}
               capacity={data.installed_capacity_kw || null}
               unit="kW"
               note="everything installed, including units that are not running — the N+1 question" />
      </div>

      {data.chillers.length > 0 && (
        <>
          <h3>Chillers</h3>
          <table>
            <thead>
              <tr><th>Unit</th><th>State</th><th>Capacity</th><th>Load</th></tr>
            </thead>
            <tbody>
              {data.chillers.map((c) => (
                <tr key={c.device_id}>
                  <td>{c.name}</td>
                  <td>
                    <span className={`chip ${c.running ? 'ok' : 'unknown'}`}>
                      {c.running ? 'running' : 'stopped'}
                    </span>
                  </td>
                  <td className="num">{c.capacity_kw?.toFixed(0) ?? '—'} kW</td>
                  <td className="num">{c.load_kw?.toFixed(0) ?? '—'} kW</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {data.loops.length > 0 && (
        <>
          <h3>Chilled water loops</h3>
          <table>
            <thead>
              <tr><th>Loop</th><th>ΔT</th><th>Flow</th><th>Heat</th><th>Verdict</th></tr>
            </thead>
            <tbody>
              {data.loops.map((l) => (
                <tr key={l.name}>
                  <td>{l.name}</td>
                  <td className="num">{l.delta_t_k?.toFixed(1) ?? '—'} K</td>
                  <td className="num">{l.flow_l_s?.toFixed(1) ?? '—'} L/s</td>
                  <td className="num">{l.heat_kw?.toFixed(0) ?? '—'} kW</td>
                  <td>
                    {l.verdict && (
                      <span className={`chip ${tone(l.verdict)}`}>
                        {l.verdict.replace(/_/g, ' ')}
                      </span>
                    )}
                    {l.note && <div className="muted small">{l.note}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">
            Heat is flow × ΔT × 4.187. A loop moving water with almost no ΔT is
            the classic low-ΔT syndrome: pumps working, little heat carried.
          </p>
        </>
      )}

      {data.data_quality.length > 0 && (
        <>
          <h3>Do the instruments agree?</h3>
          <table>
            <thead><tr><th>Check</th><th>Verdict</th><th>Detail</th></tr></thead>
            <tbody>
              {data.data_quality.map((q) => (
                <tr key={q.check}>
                  <td>{q.check.replace(/_/g, ' ')}</td>
                  <td><span className={`chip ${tone(q.verdict)}`}>{q.verdict}</span></td>
                  <td className="muted small">{q.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {data.chillers.length === 0 && (
        <p className="muted">
          No chiller is reporting. That is not a plant at rest — an idle plant
          still reports; this is an absence of readings, so check the BACnet and
          Modbus endpoints before concluding anything about the cooling.
        </p>
      )}
    </>
  );
}
