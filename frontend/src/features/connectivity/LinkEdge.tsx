import { memo } from 'react';
import {
  BaseEdge, EdgeLabelRenderer, getBezierPath, type EdgeProps,
} from '@xyflow/react';
import { useChartColors } from '../../components/seriesColors';

/** One line on the canvas, standing for one conductor or a bundle of them.
 *
 *  A/B is carried by POSITION first - the one-line puts the A column left and
 *  the B column right - then by the dash pattern, then by hue. Never by hue
 *  alone: this is the distinction that decides whether a load survives losing
 *  a feeder, and it used to be two raw hexes that a deuteranope reads as one
 *  colour.
 *
 *  A plain dash stays reserved for `down`. A B-side feeder that has failed
 *  must not look like a B-side feeder that is fine, so side B takes the chart
 *  ramp's dash-dot instead.
 */

export interface LinkEdgeData extends Record<string, unknown> {
  /** 'A', 'B', or null where the layer or the importer has no side. */
  side: string | null;
  /** Conductors behind this line. Seven between a UPS and an RPP draw as one. */
  count: number;
  downCount: number;
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
  const stroke = down ? 'var(--critical)'
    : d.side === 'A' ? colors.series[0]
    : d.side === 'B' ? colors.series[1]
    : 'var(--border-strong)';
  const dash = down ? '5 3' : d.side === 'B' ? '10 4 2 4' : undefined;

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
          strokeWidth: selected ? 2.6 : d.count > 1 ? 2 : 1.3,
          strokeDasharray: dash,
          opacity: down ? 0.85 : 1,
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
