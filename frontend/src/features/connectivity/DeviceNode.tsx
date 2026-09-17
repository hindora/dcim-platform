import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import type { TopologyNode } from '../../api/client';
import { NODE_H, NODE_W } from './layout';

/** One device on the canvas.
 *
 *  A card rather than a filled block. The simulator fills each node with its
 *  device-type colour, which works on one fixed dark canvas; this product has a
 *  light theme as its default and a palette check in CI, and forty saturated
 *  blocks on white is a different picture entirely. The colour moves to a
 *  tinted chip behind the glyph, so the class is still readable at a glance and
 *  the card itself stays neutral - which also leaves the status rail and the
 *  capacity bar somewhere to be seen.
 */

/** Device class, for the icon tint. Coarser than device_type on purpose: an
 *  operator scanning a diagram is looking for "is that the power chain or the
 *  cooling plant", not for the difference between an RPP and an MPP. */
type Klass = 'power' | 'cooling' | 'net' | 'it' | 'sensor';

const KLASS: Record<string, Klass> = {
  utility_feed: 'power', switchgear: 'power', generator: 'power', ats: 'power',
  ups: 'power', rpp: 'power', pdu: 'power', mcc: 'power', mpp: 'power',
  energy_monitor: 'power',
  chiller: 'cooling', cooling_tower: 'cooling', crah: 'cooling', cdu: 'cooling',
  pump: 'cooling', valve: 'cooling',
  router: 'net', switch: 'net', oob_switch: 'net', firewall: 'net',
  load_balancer: 'net', modbus_gateway: 'net', bacnet_router: 'net',
  server: 'it', storage: 'it',
  sensor: 'sensor',
};

/** Drawn, not a dingbat. An emoji font that fails to load leaves a grid of
 *  tofu where the estate is supposed to be, and the same glyph renders at a
 *  different size on every platform. */
function TypeGlyph({ type }: { type: string }) {
  const p = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.6,
              strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const };
  const svg = (children: React.ReactNode) => (
    <svg width="15" height="15" viewBox="0 0 16 16" aria-hidden>{children}</svg>
  );
  switch (KLASS[type]) {
    // A bolt: everything on the electrical chain.
    case 'power':
      return svg(<path d="M9 1.5 3.5 9h3.2l-.7 5.5L12.5 7H9.3L9 1.5Z"
                       fill="currentColor" stroke="none" />);
    // A snowflake, matching the alarm taxonomy's cooling glyph.
    case 'cooling':
      return svg(<g {...p}>
        <line x1="8" y1="1.5" x2="8" y2="14.5" />
        <line x1="2.4" y1="4.7" x2="13.6" y2="11.3" />
        <line x1="2.4" y1="11.3" x2="13.6" y2="4.7" />
      </g>);
    // Two nodes and a link: the fabric.
    case 'net':
      return svg(<g {...p}>
        <rect x="1.5" y="2" width="13" height="4" rx="1" />
        <rect x="1.5" y="10" width="13" height="4" rx="1" />
        <line x1="8" y1="6" x2="8" y2="10" />
      </g>);
    // A chassis with a drive bay: compute.
    case 'it':
      return svg(<g {...p}>
        <rect x="1.5" y="4" width="13" height="8" rx="1.2" />
        <line x1="4" y1="8" x2="7" y2="8" />
        <circle cx="11.5" cy="8" r="0.9" fill="currentColor" stroke="none" />
      </g>);
    // A probe on a stem.
    case 'sensor':
      return svg(<g {...p}>
        <circle cx="8" cy="11.5" r="2.6" />
        <line x1="8" y1="8.9" x2="8" y2="2" />
      </g>);
    default:
      return svg(<rect x="2.5" y="2.5" width="11" height="11" rx="1.5" {...p} />);
  }
}

function statusColor(status: string, severity: string): string {
  if (status === 'OFFLINE') return 'var(--critical)';
  if (status === 'UNKNOWN') return 'var(--unknown)';
  switch (severity) {
    case 'CRITICAL': return 'var(--critical)';
    case 'MAJOR': return 'var(--major)';
    case 'MINOR':
    case 'WARNING': return 'var(--warn)';
    default: return 'var(--ok)';
  }
}

/** Colour is never the only channel: a shape as well as a hue, because this
 *  diagram gets printed into method statements and photographed into tickets
 *  more than most screens here. */
function StatusDot({ status, severity }: { status: string; severity: string }) {
  const fill = statusColor(status, severity);
  const bad = status === 'OFFLINE' || severity === 'CRITICAL' || severity === 'MAJOR';
  const warn = severity === 'MINOR' || severity === 'WARNING';
  return (
    <svg width="9" height="9" viewBox="-5 -5 10 10" aria-hidden className="cn-status">
      {status === 'UNKNOWN'
        ? <circle r="3.4" fill="none" stroke={fill} strokeWidth="1.5" />
        : bad ? <path d="M0,-4.2 L4.2,3.2 L-4.2,3.2 Z" fill={fill} />
        : warn ? <path d="M0,-4.2 L4.2,0 L0,4.2 L-4.2,0 Z" fill={fill} />
        : <circle r="3.4" fill={fill} />}
    </svg>
  );
}

function watts(w: number): string {
  return w >= 1000 ? `${(w / 1000).toFixed(w >= 10_000 ? 0 : 1)} kW`
                   : `${Math.round(w)} W`;
}

export interface DeviceNodeData extends Record<string, unknown> {
  node: TopologyNode;
  showLoad: boolean;
}

function DeviceNode({ data, selected }: NodeProps) {
  const { node: n, showLoad } = data as unknown as DeviceNodeData;
  const rolled = n.rolled_up > 0;
  const draw = showLoad ? n.metrics.power_w : undefined;
  const rated = n.metrics.rated_power_w;
  const frac = draw != null && rated ? Math.min(1, draw / rated) : null;
  const tone = frac == null ? null
    : frac >= 0.95 ? 'var(--critical)'
    : frac >= 0.8 ? 'var(--warn)' : 'var(--accent)';

  return (
    <div
      className={[
        'cn-node',
        `is-${KLASS[n.device_type] ?? 'other'}`,
        selected ? 'is-selected' : '',
        n.depth > 0 ? 'is-outside' : '',
        n.status === 'OFFLINE' ? 'is-off' : '',
        rolled ? 'is-rolled' : '',
      ].filter(Boolean).join(' ')}
      style={{ width: NODE_W, height: NODE_H }}
    >
      {/* Connection points, invisible: the graph is derived, so there is
          nothing here for anyone to wire up by hand. They exist only to give
          React Flow somewhere to anchor an edge. */}
      <Handle type="target" position={Position.Top} className="cn-handle" />
      <Handle type="source" position={Position.Bottom} className="cn-handle" />

      <span className="cn-rail" style={{ background: statusColor(n.status, n.max_severity) }} />

      <span className="cn-chip"><TypeGlyph type={n.device_type} /></span>

      {/* The name gets the card's whole width. It used to share a row with the
          draw, which left it about 57px - enough for "PDUA-DC1-…" and not for
          which PDU. The device name is the one thing on this card that has to
          be readable, so the draw drops to the line below it. */}
      <span className="cn-body">
        <span className="cn-name" title={n.name}>{n.name}</span>
        <span className="cn-meta">
          <span className="cn-sub">
            {rolled ? `${n.rolled_up} × ${n.device_type.replace(/_/g, ' ')}`
                    : n.device_type.replace(/_/g, ' ')}
            {rolled && n.offline_count > 0 && (
              <span className="cn-off"> · {n.offline_count} off</span>
            )}
          </span>
          {draw != null && <span className="cn-load">{watts(draw)}</span>}
        </span>
      </span>

      <StatusDot status={n.status} severity={n.max_severity} />

      {draw != null && (
        // A rating nobody recorded is a hatched track with no percentage.
        // "Nobody wrote down what this is rated for" is not "it is rated for
        // zero", and a full bar on an unrated device would be a fabrication in
        // the one place fabrications get acted on.
        <span className={`cn-meter${frac == null ? ' is-unrated' : ''}`}>
          {frac != null && (
            <span className="cn-meter-fill"
                  style={{ width: `${Math.max(2, frac * 100)}%`, background: tone! }} />
          )}
        </span>
      )}
    </div>
  );
}

export default memo(DeviceNode);
