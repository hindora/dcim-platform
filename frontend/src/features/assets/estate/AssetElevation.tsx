import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import {
  api, type ElevationDevice, type RackElevation,
} from '../../../api/client';
import { humanise } from '../../../lib/format';
import { useHoverTip } from '../../../components/HoverTip';
import { rackLabel, rowLabel } from './labels';

/** What kind of thing occupies a slot, as a colour stripe. The hues are the
 *  console's own category tokens - power amber, cooling teal, network
 *  purple, compute blue, instruments grey - so the elevation speaks the same
 *  vocabulary as the alarm strip. */
const TYPE_CAT: Record<string, string> = {
  server: 'cap', storage: 'cap',
  switch: 'it', router: 'it', firewall: 'it', load_balancer: 'it',
  oob_management_switch: 'it', console_server: 'it',
  pdu: 'pwr', rack_pdu: 'pwr', rpp: 'pwr', ups: 'pwr', ats: 'pwr',
  energy_monitor: 'pwr',
  crah: 'cool', cdu: 'cool', chiller: 'cool',
  environmental_sensor: 'vis',
};

function catOf(deviceType: string): string {
  return TYPE_CAT[deviceType] ?? 'vis';
}

const LEGEND: [string, string][] = [
  ['cap', 'Compute & storage'],
  ['it', 'Network'],
  ['pwr', 'Power'],
  ['cool', 'Cooling'],
  ['vis', 'Instruments'],
];

/** The line of live readings a slot's card carries, from what the device
 *  actually reports - absent readings stay absent. */
function readings(d: ElevationDevice): string {
  const parts = [humanise(d.status)];
  if (d.power_w != null) parts.push(`${Math.round(d.power_w)} W`);
  if (d.inlet_temp_c != null) parts.push(`${d.inlet_temp_c.toFixed(1)} °C inlet`);
  if (d.cpu_util_pct != null) parts.push(`${Math.round(d.cpu_util_pct)}% CPU`);
  return parts.join(' · ');
}

/** An asset-context rack elevation.
 *
 *  A second elevation reading the same /racks/{id}/elevation endpoint as the
 *  operational one. That duplication is the price of the scope boundary
 *  (docs/22 §1, §6), and it is a fair price: this one carries things the
 *  operational view should not grow - contiguous free-space call-outs and
 *  lifecycle shading - while the operational one carries inlet temperature and
 *  severity, which mean nothing to somebody planning an install.
 *
 *  The free-block call-out is the point of the screen. "Is there room for a 4U
 *  chassis" answered by counting gaps in a picture is how machines end up
 *  ordered for racks that cannot take them.
 */
export function AssetElevation() {
  const { id = '' } = useParams();

  const { data, isLoading, error } = useQuery<RackElevation>({
    queryKey: ['rack-elevation', id],
    queryFn: () => api.rackElevation(id),
    enabled: Boolean(id),
  });
  const { bind, tipEl } = useHoverTip();

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (isLoading || !data) return <p className="muted">Loading…</p>;

  const { rack, positions, free_blocks: freeBlocks, zero_u_devices: zeroU } = data;
  const biggest = freeBlocks.reduce(
    (max, b) => (b.u_height > max ? b.u_height : max), 0);

  // Live totals off the elevation itself, so the panel and the picture
  // cannot disagree. Power-chain devices are EXCLUDED from the load sum: a
  // rack PDU's power reading is the load flowing THROUGH it - the very
  // servers already counted - and summing both doubles the rack.
  const mounted = positions.filter((s) => s.device).map((s) => s.device!);
  const everything = [...mounted, ...zeroU];
  const drawing = everything.filter((d) => catOf(d.device_type) !== 'pwr');
  const loadW = drawing.reduce((t, d) => t + (d.power_w ?? 0), 0);
  const anyPower = drawing.some((d) => d.power_w != null);
  const hottest = everything.reduce<number | null>(
    (m, d) => (d.inlet_temp_c != null && (m === null || d.inlet_temp_c > m)
      ? d.inlet_temp_c : m), null);
  const loadPct = anyPower && rack.rated_power_kw
    ? (loadW / 1000 / rack.rated_power_kw) * 100 : null;

  return (
    <>
      <p className="asset-table-note">
        {rack.room_id && (
          <Link to={`/assets/estate/rooms/${rack.room_id}`}>← {rack.room_name}</Link>
        )}
      </p>
      <h2>{rackLabel(rack.name)}</h2>
      <p className="subtitle">
        {rack.datacenter_code} · {rack.room_name} · {rowLabel(rack.row_name)} ·{' '}
        {rack.u_height}U
      </p>

      <div className="asset-cols">
        <div>
          <div className="asset-elev">
            {positions.map((slot) => {
              const d = slot.device;
              const largest = slot.free && slot.u_height === biggest && biggest > 0;
              return (
                <div
                  className={[
                    'asset-elev-slot',
                    slot.free ? 'is-free' : `cat-${catOf(d!.device_type)}`,
                    largest ? 'is-largest' : '',
                  ].filter(Boolean).join(' ')}
                  key={slot.u_start}
                  style={{ minHeight: 22 * slot.u_height }}
                  {...bind(d
                    ? <><b>{d.name}</b> · {humanise(d.device_type)}
                        <br />{readings(d)}</>
                    : <><b>{slot.u_height}U free</b> at U{slot.u_start}
                        {largest ? ' — the largest gap' : ''}</>)}
                >
                  <span className="u">
                    {slot.u_height > 1
                      ? `${slot.u_start}–${slot.u_start + slot.u_height - 1}`
                      : slot.u_start}
                  </span>
                  <span className="stripe" aria-hidden />
                  {d ? (
                    <>
                      <span className="who">
                        <Link to={`/assets/inventory/${d.id}`}>{d.name}</Link>
                      </span>
                      <span className="meta">{humanise(d.device_type)}</span>
                    </>
                  ) : (
                    <span className="who free-label">
                      {slot.u_height}U free{largest ? ' · largest' : ''}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
          <div className="asset-elev-legend">
            {LEGEND.map(([cat, label]) => (
              <span key={cat}>
                <i className={`sw cat-${cat}`} />
                {label}
              </span>
            ))}
          </div>
          {tipEl}
        </div>

        <div>
          <div className="asset-panel">
            <h3>Space</h3>
            <div className="asset-facts">
              <div className="asset-fact">
                <div className="k">Assets</div>
                <div className="v">{rack.device_count}</div>
              </div>
              <div className="asset-fact">
                <div className="k">Free</div>
                <div className="v">{rack.free_u ?? '—'}U</div>
              </div>
              <div className="asset-fact">
                <div className="k">Largest gap</div>
                {/* Total free and largest CONTIGUOUS free are different
                    numbers, and only the second answers "will a 4U fit". */}
                <div className="v">{biggest ? `${biggest}U` : 'none'}</div>
              </div>
              <div className="asset-fact">
                <div className="k">Rated</div>
                <div className="v">
                  {rack.rated_power_kw ? `${rack.rated_power_kw.toFixed(1)} kW` : '—'}
                </div>
              </div>
              <div className="asset-fact">
                <div className="k">Drawing now</div>
                <div className="v">
                  {anyPower ? `${(loadW / 1000).toFixed(2)} kW` : '—'}
                  {loadPct != null && (
                    <span className="muted"> · {Math.round(loadPct)}%</span>
                  )}
                </div>
              </div>
              <div className="asset-fact">
                <div className="k">Hottest inlet</div>
                <div className="v">
                  {hottest != null ? `${hottest.toFixed(1)} °C` : '—'}
                </div>
              </div>
            </div>

            {loadPct != null && (
              <div className="asset-bar" style={{ marginTop: 10 }}>
                <span style={{ width: `${Math.min(100, loadPct)}%` }} />
              </div>
            )}

            {freeBlocks.length > 0 && (
              <p className="asset-elev-free-note">
                Contiguous gaps:{' '}
                {freeBlocks
                  .map((b) => `${b.u_height}U at U${b.u_start}`)
                  .join(' · ')}
              </p>
            )}
          </div>

          {zeroU.length > 0 && (
            <div className="asset-panel" style={{ marginTop: 16 }}>
              <h3>Mounted at no U</h3>
              {/* Vertical PDUs and strapped-on probes are real assets with a
                  real location and no rack unit. Omitting them from the
                  elevation would make the rack's asset count disagree with
                  the inventory's. */}
              <ul className="asset-attention">
                {zeroU.map((d) => (
                  <li key={d.id}>
                    <Link to={`/assets/inventory/${d.id}`}>{d.name}</Link>
                    <span className="muted"> · {humanise(d.device_type)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
