import { useQuery } from '@tanstack/react-query';
import { humanise } from '../../lib/format';
import { Link, useParams } from 'react-router-dom';
import { useState } from 'react';
import { api, type ElevationDevice, type RackElevation as Elevation } from '../../api/client';
import { StatusChip } from '../../components/StatusChip';
import { useHoverTip } from '../../components/HoverTip';

/** Which measurement is painted onto the occupied slots.
 *
 *  A rack elevation with no overlay is a picture of some metal. The overlay is
 *  what makes it operational: the same 42 U answers "what is broken", "where is
 *  the heat" and "where is the power" depending on which question is being
 *  asked. */
type Overlay = 'status' | 'power' | 'thermal';

const OVERLAYS: { key: Overlay; label: string; hint: string }[] = [
  { key: 'status', label: 'Status', hint: 'communication state and alarm severity' },
  { key: 'power', label: 'Power', hint: 'draw per device, shaded against the busiest in this rack' },
  { key: 'thermal', label: 'Inlet temp', hint: 'intake air per device, shaded against ASHRAE A1 allowable' },
];

// ASHRAE A1 recommended intake is 18-27 C; allowable runs to 32. Shading is
// scaled to that band rather than to the rack's own spread, so a cool rack
// looks cool instead of inventing a hot spot out of a 2 degree range.
const TEMP_MIN = 18;
const TEMP_MAX = 32;

function severityClass(device: ElevationDevice): string {
  if (device.status === 'OFFLINE') return 'sev-offline';
  const sev = device.max_severity;
  if (sev === 'CRITICAL' || sev === 'MAJOR') return 'sev-major';
  if (sev === 'MINOR' || sev === 'WARNING') return 'sev-warn';
  return 'sev-ok';
}

/** 0..1 for shading, or null when the device does not report the metric.
 *  Null matters: "no data" must not render as "zero", which would show an
 *  unmonitored device as the coolest, least loaded thing in the rack. */
function overlayFraction(device: ElevationDevice, overlay: Overlay,
                         peakPower: number): number | null {
  if (overlay === 'power') {
    if (device.power_w == null) return null;
    return peakPower > 0 ? Math.min(1, device.power_w / peakPower) : 0;
  }
  if (overlay === 'thermal') {
    if (device.inlet_temp_c == null) return null;
    return Math.min(1, Math.max(0,
      (device.inlet_temp_c - TEMP_MIN) / (TEMP_MAX - TEMP_MIN)));
  }
  return null;
}

function overlayValue(device: ElevationDevice, overlay: Overlay): string {
  if (overlay === 'power') {
    return device.power_w == null ? 'no reading' : `${Math.round(device.power_w)} W`;
  }
  if (overlay === 'thermal') {
    return device.inlet_temp_c == null
      ? 'no reading' : `${device.inlet_temp_c.toFixed(1)} °C`;
  }
  return device.status;
}

export function RackElevationView() {
  const { id = '' } = useParams();
  const [overlay, setOverlay] = useState<Overlay>('status');
  const { bind, tipEl } = useHoverTip();

  const q = useQuery<Elevation>({
    queryKey: ['rack-elevation', id],
    queryFn: () => api.rackElevation(id),
    enabled: Boolean(id),
    refetchInterval: 15_000,
  });

  if (q.isLoading) return <p className="muted">Loading…</p>;
  if (q.isError || !q.data) return <p className="warn">Rack not found.</p>;

  const { rack, positions, free_blocks: freeBlocks, zero_u_devices: zeroU } = q.data;
  const occupied = positions.filter((p) => p.device);
  const peakPower = Math.max(
    0, ...occupied.map((p) => p.device?.power_w ?? 0));
  const largestFree = freeBlocks.reduce(
    (best, b) => (b.u_height > (best?.u_height ?? 0) ? b : best),
    undefined as { u_start: number; u_height: number } | undefined);

  // Top of rack first, which is how a rack is read standing in front of it.
  const ordered = [...positions].sort((a, b) => b.u_start - a.u_start);

  return (
    <div className="stack">
      <header className="rack-head">
        <div>
          <h2>{rack.name}</h2>
          <p className="muted">
            {[rack.datacenter_code, rack.room_name, rack.row_name]
              .filter(Boolean).join(' · ')}
          </p>
        </div>
        <dl className="rack-stats">
          <div><dt>Load</dt><dd>{rack.load_kw != null ? `${rack.load_kw.toFixed(1)} kW` : '—'}
            {rack.rated_power_kw ? <span className="muted"> / {rack.rated_power_kw} kW</span> : null}</dd></div>
          <div><dt>Devices</dt><dd>{rack.device_count}
            {rack.offline_count ? <span className="warn"> · {rack.offline_count} offline</span> : null}</dd></div>
          <div><dt>Max inlet</dt><dd>{rack.max_inlet_c != null ? `${rack.max_inlet_c.toFixed(1)} °C` : '—'}</dd></div>
          <div><dt>Free</dt><dd>{rack.free_u ?? 0} U
            {largestFree ? <span className="muted"> · largest {largestFree.u_height} U at U{largestFree.u_start}</span> : null}</dd></div>
        </dl>
      </header>

      <div className="overlay-picker" role="group" aria-label="Overlay">
        {OVERLAYS.map((o) => (
          <button
            key={o.key}
            type="button"
            title={o.hint}
            className={overlay === o.key ? 'active' : undefined}
            onClick={() => setOverlay(o.key)}
          >
            {o.label}
          </button>
        ))}
      </div>

      <div className="rack">
        {ordered.map((slot) => {
          const label = slot.u_height > 1
            ? `U${slot.u_start}–${slot.u_start + slot.u_height - 1}`
            : `U${slot.u_start}`;

          if (!slot.device) {
            return (
              <div key={`free-${slot.u_start}`} className="rack-slot free"
                   style={{ ['--u' as string]: slot.u_height }}>
                <span className="u-label">{label}</span>
                <span className="muted">{slot.u_height} U free</span>
              </div>
            );
          }

          const d = slot.device;
          const frac = overlayFraction(d, overlay, peakPower);
          const cls = overlay === 'status' ? severityClass(d) : 'sev-none';
          return (
            <Link
              key={d.id}
              to={`/devices/${d.id}`}
              className={`rack-slot filled ${cls}`}
              style={{
                ['--u' as string]: slot.u_height,
                // Only paint when there is a reading. An unshaded block with
                // "no reading" beside it is honest; a pale one implies a low
                // measurement nobody took.
                ['--fill' as string]: frac == null ? '0' : String(frac),
              }}
              {...bind(<><b>{d.name}</b> · {humanise(d.device_type)} ·{' '}
                {overlayValue(d, overlay)}</>)}
            >
              <span className="u-label">{label}</span>
              <span className="rack-name">{d.name}</span>
              <span className="rack-meta">
                {overlay === 'status'
                  ? <StatusChip status={d.status} />
                  : <span className={frac == null ? 'muted' : undefined}>
                      {overlayValue(d, overlay)}
                    </span>}
              </span>
            </Link>
          );
        })}
      </div>
      {tipEl}

      {zeroU.length > 0 && (
        <section>
          <h3>Zero-U devices</h3>
          <p className="muted">
            Mounted in the side channels rather than on the rails — vertical PDUs
            and strapped-on probes. They are in the rack but at no rack unit, so
            placing them in the grid above would imply a position they do not have.
          </p>
          <ul className="zero-u">
            {zeroU.map((d) => (
              <li key={d.id}>
                <Link to={`/devices/${d.id}`}>{d.name}</Link>
                <span className="muted"> · {humanise(d.device_type)} · </span>
                <StatusChip status={d.status} />
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
