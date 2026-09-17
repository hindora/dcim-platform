import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Background, BackgroundVariant, MiniMap, ReactFlow, ReactFlowProvider,
  useEdgesState, useNodesState, useReactFlow, useViewport,
  type Edge, type Node,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import type { TopologyNode } from '../../api/client';
import DeviceNode from './DeviceNode';
import LinkEdge from './LinkEdge';
import { NODE_H, NODE_W, type CollapsedEdge, type Placed } from './layout';
import { cutWithin, verdictFor, type ImpactView } from './impact';

/** The connectivity canvas — now the page rather than a panel on it.
 *
 *  React Flow renders it; the POSITIONS are still ours. React Flow lays nothing
 *  out, and the arrangement is the domain content: source at the top, A column
 *  left, B column right, dual-fed loads on the gutter between them, and the
 *  cooling loop bent round a U.
 *
 *  The rule that survives every rewrite of this file: A LIVE UPDATE MUST NOT
 *  MOVE A NODE. Positions are rebuilt only when the layout key changes; a poll
 *  returning the same graph with new statuses walks the existing nodes and
 *  swaps their `data`. That also means a node somebody dragged stays put.
 */

/** Frame the graph, but never below the zoom at which a device name stops
 *  being a word. Below this the view lands on the graph and pans, which is how
 *  every map behaves. */
const FIT = { padding: 0.16, minZoom: 0.62, duration: 320 } as const;

const nodeTypes = { device: DeviceNode };
const edgeTypes = { link: LinkEdge };

export interface Placement {
  placed: Placed[];
  width: number;
  height: number;
}

function nodeData(node: Placed['node'], showLoad: boolean,
                  impact: ImpactView | null) {
  return {
    node,
    showLoad,
    verdict: impact ? verdictFor(node, impact) : null,
    cutWithin: impact ? cutWithin(node, impact) : 0,
    isCandidate: impact
      ? node.id === impact.candidate || node.member_ids.includes(impact.candidate)
      : false,
  };
}

function buildNodes(placement: Placement, showLoad: boolean,
                    selected: string | null, impact: ImpactView | null): Node[] {
  return placement.placed.map((p) => ({
    id: p.node.id,
    type: 'device',
    position: { x: p.x, y: p.y },
    data: nodeData(p.node, showLoad, impact),
    selected: p.node.id === selected,
    // Dragging rearranges the picture, which is welcome; connecting two nodes
    // by hand would invent a cable, which is not.
    connectable: false,
    width: NODE_W,
    height: NODE_H,
  }));
}

function buildEdges(edges: CollapsedEdge[], layer: string,
                    animated: boolean): Edge[] {
  return edges.map((e) => ({
    id: `${e.source}>${e.target}`,
    source: e.source,
    target: e.target,
    type: 'link',
    data: {
      side: e.sides.length === 1 ? e.sides[0] : null,
      count: e.count,
      downCount: e.downCount,
      layer,
      animated,
    },
  }));
}

function Icon({ d }: { d: string }) {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" aria-hidden
         fill="none" stroke="currentColor" strokeWidth="2"
         strokeLinecap="round" strokeLinejoin="round">
      <path d={d} />
    </svg>
  );
}

const PATH = {
  zoomIn: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14M20 20l-4-4M11 8v6M8 11h6',
  zoomOut: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14M20 20l-4-4M8 11h6',
  fit: 'M3 8V5a2 2 0 0 1 2-2h3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M21 16v3a2 2 0 0 1-2 2h-3',
  map: 'M3 6l6-3 6 3 6-3v15l-6 3-6-3-6 3zM9 3v15M15 6v15',
};

/** The zoom, printed. A canvas that can be panned off its own content has to
 *  say where it is, and the percentage is also the fastest way back: clicking
 *  it fits. */
function ZoomLevel({ onFit }: { onFit: () => void }) {
  const { zoom } = useViewport();
  return (
    <button type="button" className="cn-zoom" title="Fit to view" onClick={onFit}>
      {Math.round(zoom * 100)}%
    </button>
  );
}

function Toolbar({ onFit, showMap, onToggleMap, canMap }: {
  onFit: () => void;
  showMap: boolean;
  onToggleMap: () => void;
  canMap: boolean;
}) {
  const { zoomIn, zoomOut } = useReactFlow();
  return (
    <div className="cn-float cn-toolbar">
      <button type="button" title="Zoom out" onClick={() => zoomOut({ duration: 180 })}>
        <Icon d={PATH.zoomOut} />
      </button>
      <ZoomLevel onFit={onFit} />
      <button type="button" title="Zoom in" onClick={() => zoomIn({ duration: 180 })}>
        <Icon d={PATH.zoomIn} />
      </button>
      <span className="cn-sep" />
      <button type="button" title="Fit to view" onClick={onFit}>
        <Icon d={PATH.fit} />
      </button>
      {canMap && (
        <button type="button" title="Minimap" aria-pressed={showMap}
                className={showMap ? 'is-on' : undefined} onClick={onToggleMap}>
          <Icon d={PATH.map} />
        </button>
      )}
    </div>
  );
}

function Flow({ placement, edges, layer, layoutKey, selected, onSelect, impact,
                children }: {
  placement: Placement;
  edges: CollapsedEdge[];
  layer: string;
  /** Changes only when the STRUCTURE does. Positions rebuild on it and on
   *  nothing else, which is what keeps a status poll from moving the picture. */
  layoutKey: string;
  selected: string | null;
  onSelect: (node: TopologyNode | null) => void;
  /** Non-null while a removal is being simulated. */
  impact: ImpactView | null;
  /** The floating chrome. Rendered inside the canvas so it sits over the
   *  graph, and after ReactFlow so it stacks above without a z-index war. */
  children?: React.ReactNode;
}) {
  const showLoad = layer === 'power';
  const flowing = layer === 'cooling';

  const [nodes, setNodes, onNodesChange] = useNodesState<Node>(
    buildNodes(placement, showLoad, selected, impact));
  const [rfEdges, setEdges, onEdgesChange] = useEdgesState<Edge>(
    buildEdges(edges, layer, flowing));

  const { fitView } = useReactFlow();
  const lastLayout = useRef(layoutKey);
  const [showMap, setShowMap] = useState(false);

  useEffect(() => {
    if (lastLayout.current !== layoutKey) {
      lastLayout.current = layoutKey;
      setNodes(buildNodes(placement, showLoad, selected, impact));
      setEdges(buildEdges(edges, layer, flowing));
      const t = window.setTimeout(() => fitView(FIT), 30);
      return () => window.clearTimeout(t);
    }
    // Same structure, newer state: replace the data, keep every position -
    // including one the operator dragged.
    const byId = new Map(placement.placed.map((p) => [p.node.id, p.node]));
    setNodes((nds) => nds.map((n) => {
      const fresh = byId.get(n.id);
      return fresh
        ? { ...n, data: nodeData(fresh, showLoad, impact), selected: n.id === selected }
        : n;
    }));
    setEdges(buildEdges(edges, layer, flowing));
    return undefined;
  }, [layoutKey, placement, edges, layer, showLoad, flowing, selected, impact,
      setNodes, setEdges, fitView]);

  const onNodeClick = useCallback((_: unknown, n: Node) => {
    onSelect((n.data as { node: TopologyNode }).node);
  }, [onSelect]);

  const canMap = placement.placed.length > 25;

  // The flow animation is a per-frame stroke repaint and it fights the
  // viewport transform while somebody pans; the whole canvas judders. Paused
  // by toggling a class directly rather than through state, so a pan does not
  // re-render every node on the way past.
  const wrap = useRef<HTMLDivElement>(null);
  const idle = useRef<number | undefined>(undefined);
  const onMoveStart = useCallback(() => {
    if (idle.current) window.clearTimeout(idle.current);
    wrap.current?.classList.add('is-moving');
  }, []);
  const onMoveEnd = useCallback(() => {
    idle.current = window.setTimeout(
      () => wrap.current?.classList.remove('is-moving'), 140);
  }, []);

  return (
    <div className="cn-canvas" ref={wrap}>
      <ReactFlow
        nodes={nodes}
        edges={rfEdges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={onNodeClick}
        onPaneClick={() => onSelect(null)}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        nodesConnectable={false}
        elementsSelectable
        fitView
        fitViewOptions={FIT}
        minZoom={0.08}
        maxZoom={2.2}
        onlyRenderVisibleElements
        onMoveStart={onMoveStart}
        onMoveEnd={onMoveEnd}
        proOptions={{ hideAttribution: true }}
        aria-label={`${layer} connectivity, ${placement.placed.length} devices`}
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1}
                    color="var(--cn-dot)" />
        {canMap && showMap && (
          <MiniMap pannable zoomable className="cn-minimap"
                   maskColor="var(--cn-mask)"
                   nodeColor={() => 'var(--border-strong)'} />
        )}
      </ReactFlow>

      <Toolbar onFit={() => fitView(FIT)}
               showMap={showMap} canMap={canMap}
               onToggleMap={() => setShowMap((v) => !v)} />

      {children}
    </div>
  );
}

export function Canvas(props: Parameters<typeof Flow>[0]) {
  // One provider per canvas. A second instance gets its own viewport, which is
  // what lets someone zoom into one without dragging the other underneath it.
  return (
    <ReactFlowProvider>
      <Flow {...props} />
    </ReactFlowProvider>
  );
}
