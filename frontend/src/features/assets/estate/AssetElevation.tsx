import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { api, type RackElevation } from '../../../api/client';
import { humanise } from '../../../lib/format';
import { useHoverTip } from '../../../components/HoverTip';
import { rackLabel, rowLabel } from './labels';

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
              const label = slot.device
                ? slot.device.name
                : `${slot.u_height}U free`;
              return (
                <div
                  className={`asset-elev-slot${slot.free ? ' is-free' : ''}`}
                  key={slot.u_start}
                  style={{ minHeight: 20 * slot.u_height }}
                  {...bind(slot.device
                    ? <><b>{slot.device.name}</b> · {humanise(slot.device.device_type)}</>
                    : 'Free')}
                >
                  <span className="u">
                    {slot.u_height > 1
                      ? `${slot.u_start}-${slot.u_start + slot.u_height - 1}`
                      : slot.u_start}
                  </span>
                  <span className="who">
                    {slot.device ? (
                      <Link to={`/assets/inventory/${slot.device.id}`}>{label}</Link>
                    ) : (
                      label
                    )}
                  </span>
                </div>
              );
            })}
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
            </div>

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
