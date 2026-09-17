import { memo } from 'react';
import {
  BaseEdge, EdgeLabelRenderer, getBezierPath, type EdgeProps,
} from '@xyflow/react';
import { useChartColors } from '../../components/seriesColors';

/** One line on the canvas, standing for one conductor or a bundle of them.
 *
 *  Drawn the way the simulator draws them: a bezier that leaves the bottom of
 *  a node and arrives at the top of the next, coloured by LAYER, with the
 *  cooling loop's dashes marching in the direction the water flows.
 *
 *  One departure, and it is deliberate. The simulator shows every layer at
 *  once, so layer IS the only thing its edge colour can carry. This page shows
 *  one layer at a time, which frees the colour for something the simulator has
 *  no room for: on a layer with labelled sides, A and B take the chart ramp on
 *  top of the layer's hue. Which side a feeder is on decides whether a load
 *  survives losing one, that ramp is the colour-blind-safe one the palette
 *  check enforces, and hue is never the only channel anyway - A is solid, B is
 *  dash-dot, and the one-line puts them in separate columns.
 */

const LAYER_STROKE: Record<string, string> = {
  network: 'var(--layer-prod)',
  production: 'var(--layer-prod)',
  management: 'var(--layer-mgmt)',
  power: 'var(--layer-power)',
  cooling: 'var(--layer-cooling)',
  fieldbus: 'var(--layer-fieldbus)',
};

export interface LinkEdgeData extends Record<string, unknown> {
  /** 'A', 'B', or null where the layer or the importer has no side. */
  side: string | null;
  /** Conductors behind this line. Seven between a UPS and an RPP draw as one. */
  count: number;
  downCount: number;
  layer: string;
  /** Cooling flows, and a flowing pipe is worth animating; a cord is not. */
  animated: boolean;
}

function LinkEdge(props: EdgeProps) {
  const {
    id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition,
    data, selected, markerEnd,
  } = props;
  const d = (data ?? {}) as LinkEdgeData;
  const colors = useChartColors();

  const down = d.downCount > 0 && d.downCount === d.count;
  const layerStroke = LAYER_STROKE[d.layer] ?? 'var(--layer-prod)';
  const stroke = down ? 'var(--critical)'
    : d.side === 'A' ? colors.series[0]
    : d.side === 'B' ? colors.series[1]
    : layerStroke;
  // A plain dash is reserved for `down`: a B-side feeder that has failed must
  // not look like a B-side feeder that is fine.
  const dash = down ? '5 3'
    : d.side === 'B' ? '10 4 2 4'
    : d.animated ? '7 5' : undefined;

  const [path, labelX, labelY] = getBezierPath({
    sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition,
  });

  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        markerEnd={markerEnd}
        className={d.animated && !down ? 'cn-edge is-flowing' : 'cn-edge'}
        style={{
          stroke,
          strokeWidth: selected ? 2.8 : d.count > 1 ? 2.1 : 1.4,
          strokeDasharray: dash,
          opacity: down ? 0.9 : d.layer === 'management' ? 0.75 : 1,
        }}
      />

      {/* A bundle says how many it stands for, and a failed one says so in
          words. Both ride in the DOM rather than as SVG text so they stay
          upright and legible at any zoom. */}
      {(down || d.count > 1) && (
        <EdgeLabelRenderer>
          <div
            className={`cn-edge-label${down ? ' is-down' : ''}`}
            style={{ transform: `translate(-50%,-50%) translate(${labelX}px,${labelY}px)` }}
          >
            {down ? 'DOWN' : `×${d.count}`}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

export default memo(LinkEdge);
