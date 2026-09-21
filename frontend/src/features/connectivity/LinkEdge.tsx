import { memo } from 'react';
import {
  BaseEdge, EdgeLabelRenderer, getBezierPath, type EdgeProps,
} from '@xyflow/react';

/** One line on the canvas, standing for one conductor or a bundle of them.
 *
 *  Drawn the way the simulator draws them: a bezier that leaves the bottom of
 *  a node and arrives at the top of the next, coloured by LAYER, with the
 *  cooling loop's dashes marching in the direction the water flows.
 *
 *  ONE LAYER, ONE COLOUR. The A and B feeders on the power layer used to take
 *  the chart ramp's first two colours on top of the layer's hue, which made a
 *  power diagram three colours before anything was wrong with it and left the
 *  layer's own amber reading as "neither side". Side is still on the canvas,
 *  and on the channel that always carried it better: the one-line puts A down
 *  the left, B down the right and the dual-fed equipment between them, and the
 *  selection drawer names the side of every hop.
 *
 *  A DOWN line stays critical red. That is not a label on a conductor, it is
 *  the state of one, and a fault that draws the same as a healthy cord is the
 *  one thing this canvas must never do.
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

  const down = d.downCount > 0 && d.downCount === d.count;
  const layerStroke = LAYER_STROKE[d.layer] ?? 'var(--layer-prod)';
  const stroke = down ? 'var(--critical)' : layerStroke;

  // A DASH MEANS FLOW, and nothing else. It was doing three jobs at once -
  // side B, a failed link, and the cooling loop - and an idiom that means
  // three things means none of them. It belongs to the loop, where it is
  // animated and reads as water moving; everything else is a solid line.
  //
  // What the other two lose it in, they keep elsewhere. A failed link is
  // critical red AND carries the word DOWN. Side B sits in its own column on
  // the one-line - position was always the primary channel there.
  const dash = d.animated && !down ? '7 5' : undefined;

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
