import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import type { TopologyNode } from '../../api/client';
import { NODE_H, NODE_W } from './layout';
import type { Verdict } from './impact';

/** One device on the canvas.
 *
 *  Filled with its device-type colour, the way the simulator draws it, so the
 *  same machine is the same colour in both products: brass is electrical, teal
 *  is cooling, blue is fabric, and nobody has to learn it twice. Every fill is
 *  solved to roughly one luminance, which is what lets a FAULT win - a canvas
 *  of healthy equipment has no hot spots for a red node to compete with.
 *
 *  The fills are identical in both themes. They are saturated and dark enough
 *  to carry white text on any ground, and a second set for the light palette
 *  would be a second identity for the same machine.
 */

/** device_type -> fill. The token values live in index.css; see the note there
 *  about why they are copied from the simulator rather than re-derived. */
const FILL: Record<string, string> = {
  switch: 'var(--node-switch)',
  router: 'var(--node-router)',
  server: 'var(--node-server)',
  storage: 'var(--node-storage)',
  firewall: 'var(--node-firewall)',
  load_balancer: 'var(--node-lb)',
  oob_switch: 'var(--node-oob)',
  utility_feed: 'var(--node-utility-feed)',
  switchgear: 'var(--node-switchgear)',
  mcc: 'var(--node-mcc)',
  ats: 'var(--node-ats)',
  generator: 'var(--node-generator)',
  ups: 'var(--node-ups)',
  rpp: 'var(--node-rpp)',
  pdu: 'var(--node-pdu)',
  mpp: 'var(--node-mpp)',
  crah: 'var(--node-crah)',
  chiller: 'var(--node-chiller)',
  pump: 'var(--node-pump)',
  cooling_tower: 'var(--node-cooling-tower)',
  valve: 'var(--node-valve)',
  cdu: 'var(--node-cdu)',
  sensor: 'var(--node-sensor)',
  energy_monitor: 'var(--node-energy-monitor)',
  modbus_gateway: 'var(--node-modbus-gateway)',
  bacnet_router: 'var(--node-bacnet-router)',
};

/** Exported so the side list can wear the same identity as the canvas. A
 *  second colour table for the same machine is a second identity. */
export const fillOf = (t: string) => FILL[t] ?? 'var(--node-default)';

/** Class, for the glyph only. Coarser than device_type on purpose: scanning a
 *  diagram you look for "is that the power chain or the cooling plant", not
 *  for the difference between an RPP and an MPP. */
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
 *  tofu where the estate is supposed to be, and renders at a different size on
 *  every platform. */
function TypeGlyph({ type }: { type: string }) {
  const p = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.7,
              strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const };
  const svg = (children: React.ReactNode) => (
    <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden>{children}</svg>
  );
  switch (KLASS[type]) {
    case 'power':
      return svg(<path d="M9 1.5 3.5 9h3.2l-.7 5.5L12.5 7H9.3L9 1.5Z"
                       fill="currentColor" stroke="none" />);
    case 'cooling':
      return svg(<g {...p}>
        <line x1="8" y1="1.5" x2="8" y2="14.5" />
        <line x1="2.4" y1="4.7" x2="13.6" y2="11.3" />
        <line x1="2.4" y1="11.3" x2="13.6" y2="4.7" />
      </g>);
    case 'net':
      return svg(<g {...p}>
        <rect x="1.5" y="2" width="13" height="4" rx="1" />
        <rect x="1.5" y="10" width="13" height="4" rx="1" />
        <line x1="8" y1="6" x2="8" y2="10" />
      </g>);
    case 'it':
      return svg(<g {...p}>
        <rect x="1.5" y="4" width="13" height="8" rx="1.2" />
        <line x1="4" y1="8" x2="7" y2="8" />
        <circle cx="11.5" cy="8" r="0.9" fill="currentColor" stroke="none" />
      </g>);
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

/** Colour is never the only channel. The pip carries a shape too, because this
 *  diagram gets printed into method statements and photographed into tickets
 *  more than most screens here. */
function StatusPip({ status, severity }: { status: string; severity: string }) {
  const fill = statusColor(status, severity);
  const bad = status === 'OFFLINE' || severity === 'CRITICAL' || severity === 'MAJOR';
  const warn = severity === 'MINOR' || severity === 'WARNING';
  return (
    <svg width="9" height="9" viewBox="-5 -5 10 10" aria-hidden className="cn-pip">
      {status === 'UNKNOWN'
        ? <circle r="3.3" fill="none" stroke={fill} strokeWidth="1.6" />
        : bad ? <path d="M0,-4.2 L4.2,3.2 L-4.2,3.2 Z" fill={fill} />
        : warn ? <path d="M0,-4.2 L4.2,0 L0,4.2 L-4.2,0 Z" fill={fill} />
        : <circle r="3.3" fill={fill} />}
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
  verdict: Verdict;
  cutWithin: number;
  isCandidate: boolean;
  /** The room box this device belongs to, on the site view. */
  roomId?: string;
}

function DeviceNode({ data, selected }: NodeProps) {
  const { node: n, showLoad, verdict, cutWithin, isCandidate, roomId } =
    data as unknown as DeviceNodeData;
  const rolled = n.rolled_up > 0;
  const own = showLoad ? n.metrics.power_w : undefined;
  // A panel of breakers and a transfer switch do not meter themselves. What
  // they draw is taken from the meter on their bus, or from the sum of what
  // they feed, and it is drawn with a ~ so it can never be read as the
  // device's own telemetry.
  const borrowed = showLoad && own == null ? n.derived_power_w : null;
  const draw = own ?? borrowed ?? undefined;
  const rated = n.metrics.rated_power_w;
  const frac = draw != null && rated ? Math.min(1, draw / rated) : null;
  const drawTitle = borrowed != null
    ? `${watts(borrowed)} — not measured here: ${
        n.derived_power_kind === 'metered'
          ? `metered by ${n.derived_power_from}`
          : `the sum of what it feeds (${n.derived_power_from})`}`
    : undefined;
  const tone = frac == null ? null
    : frac >= 0.95 ? 'var(--critical)'
    : frac >= 0.8 ? 'var(--warn)' : 'var(--on-solid)';

  const fill = fillOf(n.device_type);

  return (
    <div
      // Which room box owns it on the site view. Carried into the DOM because
      // it decides what a room drag takes with it, and a rule that can only
      // be checked by eye is a rule nobody can test.
      data-room={roomId}
      className={[
        'cn-node',
        selected ? 'is-selected' : '',
        n.depth > 0 ? 'is-outside' : '',
        n.status === 'OFFLINE' ? 'is-off' : '',
        rolled ? 'is-rolled' : '',
        verdict ? `is-${verdict}` : '',
        isCandidate ? 'is-candidate' : '',
      ].filter(Boolean).join(' ')}
      style={{
        width: NODE_W,
        height: NODE_H,
        background: fill,
        // A dashed border in the fill colour would be invisible, so the
        // out-of-scope variant borrows the label colour instead.
        borderColor: n.depth > 0 ? 'var(--node-label)' : fill,
      }}
    >
      {/* The graph is derived, so there is nothing here to wire by hand. These
          exist only to give React Flow somewhere to anchor an edge. */}
      <Handle type="target" position={Position.Top} className="cn-handle" />
      <Handle type="source" position={Position.Bottom} className="cn-handle" />

      {/* Stacked and centred, the way the simulator draws one: glyph, name,
          one line of detail. A wide horizontal card carried more text and cost
          twice the canvas per device, which is the wrong trade on a diagram
          whose whole problem is fitting a hall on a screen. */}
      <span className="cn-glyph"><TypeGlyph type={n.device_type} /></span>

      <span className="cn-name" title={n.name}>{n.name}</span>

      {/* One line, and the load wins it where there is one: on the power layer
          the draw is the number being read, and the type is already in the
          colour and the glyph. */}
      <span className={`cn-sub${borrowed != null ? ' is-borrowed' : ''}`}
            title={drawTitle}>
        {draw != null ? `${borrowed != null ? '~' : ''}${watts(draw)}`
          : rolled ? `${n.rolled_up} × ${n.device_type.replace(/_/g, ' ')}`
          : n.device_type.replace(/_/g, ' ')}
        {rolled && draw != null && ` · ${n.rolled_up}`}
        {rolled && n.offline_count > 0 && (
          <span className="cn-off"> · {n.offline_count} off</span>
        )}
      </span>

      <StatusPip status={n.status} severity={n.max_severity} />

      {draw != null && (
        // A rating nobody recorded is a hatched groove with no fill. "No limit
        // recorded" is not 0 %, and a full bar on an unrated device would be a
        // fabrication in the one place fabrications get acted on.
        <span className={`cn-meter${frac == null ? ' is-unrated' : ''}`
                         + `${borrowed != null ? ' is-borrowed' : ''}`}>
          {frac != null && (
            <span className="cn-meter-fill"
                  style={{ width: `${Math.max(2, frac * 100)}%`, background: tone! }} />
          )}
        </span>
      )}

      {/* While a removal is simulated the verdict outranks everything else on
          the card: it is the reason the operator is looking at it. */}
      {verdict && (
        <span className="cn-verdict">
          {verdict === 'cut' ? 'goes dark'
            : verdict === 'partial' ? `${cutWithin} of ${n.rolled_up} dark`
            : 'loses a side'}
        </span>
      )}
      {isCandidate && <span className="cn-verdict is-candidate">removed</span>}
    </div>
  );
}

export default memo(DeviceNode);
