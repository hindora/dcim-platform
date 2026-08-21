import { useQuery } from '@tanstack/react-query';
import { api, type RoomSummary, type ThermalRoom } from '../../api/client';

/** Rack ΔT, hot spots, and the distinction that decides where an engineer goes.
 *
 *  A CRAH with a high RETURN is working and being fed hot air by the room -
 *  containment, load, bypass; the fix is on the floor. A CRAH with a high
 *  SUPPLY has failed to cool - valve, coil, flow; the fix is at the machine.
 *  The two look identical on a dashboard that only plots "CRAH temperature",
 *  so this view never shows one without the other and labels the state in
 *  words.
 *
 *  Hot spots are relative to the room and a room-wide event is not, which is
 *  why they are separate findings: when every rack rises together nothing is
 *  relatively hot, and a display that only had hot spots would go quiet during
 *  the largest thermal event a room can have.
 */

const ASHRAE_RECOMMENDED = 27;
const ASHRAE_ALLOWABLE = 32;

const STATE_TONE: Record<string, string> = {
  ok: 'ok',
  stopped: 'unknown',
  high_return: 'warn',
  high_supply: 'critical',
};

function InletBar({ value }: { value: number | null }) {
  if (value === null) return <span className="muted">—</span>;
  // Scaled across the working range of a cold aisle, not from zero: 18 to 32 is
  // where every decision lives, and a bar from 0 makes a 5 K excursion look
  // like a rounding error.
  const pct = Math.max(0, Math.min(100, ((value - 15) / (35 - 15)) * 100));
  const tone = value > ASHRAE_ALLOWABLE ? 'critical'
    : value > ASHRAE_RECOMMENDED ? 'warn' : 'ok';
  return (
    <div className="inline-bar">
      <div className="meter-track">
        <div className={`meter-fill ${tone}`} style={{ width: `${pct}%` }} />
      </div>
      <span className={tone}>{value.toFixed(1)} °C</span>
    </div>
  );
}

export function ThermalView({ room }: { room: RoomSummary }) {
  const { data, error, isLoading } = useQuery<ThermalRoom>({
    queryKey: ['thermal', room.id],
    queryFn: () => api.thermal(room.id),
    refetchInterval: 60_000,
  });

  if (isLoading) return <p className="muted">Loading…</p>;
  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (!data) return null;

  // A unit that reported nothing is not a unit in good order. Counting only
  // the ones in a named fault state renders "0 units in fault" over a room
  // where every sensor is silent, which is the same absence-as-zero mistake the
  // backend refuses to make.
  const measuredUnits = data.crah_units.filter(
    (u) => u.supply_c !== null || u.return_c !== null);
  const silentUnits = data.crah_units.length - measuredUnits.length;
  const noData = data.inlet_p90_c === null && measuredUnits.length === 0;

  return (
    <>
      {data.thermal_event && (
        <div className="banner">
          <strong>{data.thermal_event.type.replace(/_/g, ' ')}</strong> —{' '}
          {data.thermal_event.summary}
          {data.thermal_event.hottest && ` (hottest: ${data.thermal_event.hottest})`}
        </div>
      )}

      <div className="tiles">
        <div className="tile">
          <div className="label">Inlet p90</div>
          <div className="value">{data.inlet_p90_c?.toFixed(1) ?? '—'} °C</div>
          <div className="detail">
            recommended limit {ASHRAE_RECOMMENDED} °C (ASHRAE A1)
          </div>
        </div>
        <div className="tile">
          <div className="label">Hot spots</div>
          <div className="value">{data.hot_spot_count}</div>
          <div className="detail">
            racks above {data.hot_spot_threshold_c?.toFixed(1) ?? '—'} °C for the
            whole window
          </div>
        </div>
        <div className="tile">
          <div className="label">Room ΔT</div>
          <div className="value">{data.room_delta_t_k?.toFixed(1) ?? '—'} K</div>
          <div className="detail">CRAH return minus supply</div>
        </div>
        <div className="tile">
          <div className="label">Units in fault</div>
          <div className="value">
            {measuredUnits.length === 0
              ? '—'
              : data.units_high_supply + data.units_high_return}
          </div>
          <div className="detail">
            {measuredUnits.length === 0
              ? `${data.crah_units.length} unit(s), none reporting`
              : `${data.units_high_supply} not cooling · ` +
                `${data.units_high_return} fed hot air`}
          </div>
        </div>
      </div>

      {noData ? (
        <div className="banner soft">
          No rack or CRAH readings in the last 30 minutes. The room is not cool,
          it is unmeasured — the zeroes above are an absence of data, not a
          healthy room. Check the collector and the BACnet endpoints.
        </div>
      ) : silentUnits > 0 && (
        <div className="banner soft">
          {silentUnits} of {data.crah_units.length} CRAH units reported no air
          temperatures. Whatever those units are doing is not visible here.
        </div>
      )}

      {data.crah_units.length > 0 && (
        <>
          <h3>CRAH units</h3>
          <table>
            <thead>
              <tr>
                <th>Unit</th><th>State</th><th>Supply</th><th>Return</th>
                <th>Setpoint</th><th>ΔT</th><th>What it means</th>
              </tr>
            </thead>
            <tbody>
              {data.crah_units.map((u) => (
                <tr key={u.device_id}>
                  <td>{u.name}</td>
                  <td><span className={`chip ${STATE_TONE[u.state] ?? 'unknown'}`}>
                    {u.state.replace(/_/g, ' ')}
                  </span></td>
                  <td className="num">{u.supply_c ?? '—'}</td>
                  <td className="num">{u.return_c ?? '—'}</td>
                  <td className="num">{u.setpoint_c ?? '—'}</td>
                  <td className="num">{u.delta_t_k ?? '—'}</td>
                  <td className="muted small">{u.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {data.racks.length > 0 && (
        <>
          <h3>Racks, hottest first</h3>
          <table>
            <thead>
              <tr>
                <th>Rack</th><th>Inlet</th><th>Exhaust</th><th>ΔT</th><th></th>
              </tr>
            </thead>
            <tbody>
              {data.racks.map((r) => (
                <tr key={r.rack_id}>
                  <td>{r.name}</td>
                  <td style={{ width: 220 }}><InletBar value={r.inlet_mean_c} /></td>
                  <td className="num">{r.exhaust_mean_c?.toFixed(1) ?? '—'}</td>
                  <td className="num">{r.delta_t_k?.toFixed(1) ?? '—'} K</td>
                  <td>
                    {r.above_allowable
                      ? <span className="chip critical">above allowable</span>
                      : r.above_recommended
                        ? <span className="chip warn">above recommended</span>
                        : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">
            Rack ΔT is exhaust minus intake. A low ΔT with a warm room is usually
            bypass air — cold supply reaching the return without passing through
            a server — not a cooling shortage.
          </p>
        </>
      )}
    </>
  );
}
