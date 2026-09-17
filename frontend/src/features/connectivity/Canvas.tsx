import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Background, BackgroundVariant, MiniMap, ReactFlow, ReactFlowProvider,
  useEdgesState, useNodesState, useReactFlow,
  type Edge, type Node,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import type { TopologyNode } from '../../api/client';
import DeviceNode from './DeviceNode';
import LinkEdge from './LinkEdge';
import { NODE_H, NODE_W, type CollapsedEdge, type Placed } from './layout';
import { cutWithin, verdictFor, type ImpactView } from './impact';

/** The connectivity canvas.
 *
 *  React Flow renders it; the POSITIONS are still ours. React Flow lays nothing
 *  out - every layout example it ships pairs it with dagre or elk - and the
 *  arrangement is the domain content here: source at the top, A column left, B
 *  column right, dual-fed loads on the gutter between them. What it buys is the
 *  things a hand-rolled SVG had to fake: real pan and zoom, nodes that are DOM
 *  elements and therefore themeable and focusable, edge labels that stay
 *  upright at any zoom, and a minimap.
 *
 *  The one rule that survives from the SVG version unchanged: A LIVE UPDATE
 *  MUST NOT MOVE A NODE. Positions are rebuilt only when the layout key
 *  changes; a poll that returns the same graph with new statuses walks the
 *  existing nodes and replaces their `data`, leaving `position` alone. That
 *  also means a node the operator has dragged stays where they put it.
 */

/** Frame the graph, but never below the zoom at which a device name stops
 *  being a word. Fitting a two-thousand-pixel-wide hall into a panel puts it
 *  at about 0.45, where a 10.5px name is five pixels of grey - the whole
 *  picture visible and none of it readable. Below this the view lands at the
 *  top-left of the graph and pans, which is how every map behaves. */
const FIT = { padding: 0.14, minZoom: 0.62, duration: 320 } as const;

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
    // The graph is derived from the connection table. Dragging rearranges the
    // picture, which is welcome; connecting two nodes by hand would invent a
    // cable, which is not.
    connectable: false,
    width: NODE_W,
    height: NODE_H,
  }));
}

function buildEdges(edges: CollapsedEdge[], animated: boolean): Edge[] {
  return edges.map((e) => ({
    id: `${e.source}>${e.target}`,
    source: e.source,
    target: e.target,
    type: 'link',
    data: {
      side: e.sides.length === 1 ? e.sides[0] : null,
      count: e.count,
      downCount: e.downCount,
      animated,
    },
  }));
}

function Icon({ d, filled }: { d: string; filled?: boolean }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden
         fill={filled ? 'currentColor' : 'none'} stroke="currentColor"
         strokeWidth="2.1" strokeLinecap="round" strokeLinejoin="round">
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

function Toolbar({ onFit, showMap, onToggleMap, canMap }: {
  onFit: () => void;
  showMap: boolean;
  onToggleMap: () => void;
  canMap: boolean;
}) {
  const { zoomIn, zoomOut } = useReactFlow();
  return (
    <div className="cn-toolbar">
      <button type="button" title="Zoom in" onClick={() => zoomIn({ duration: 180 })}>
        <Icon d={PATH.zoomIn} />
      </button>
      <button type="button" title="Zoom out" onClick={() => zoomOut({ duration: 180 })}>
        <Icon d={PATH.zoomOut} />
      </button>
      <span className="cn-toolbar-sep" />
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

function Flow({ placement, edges, layer, layoutKey, selected, onSelect, impact }: {
  placement: Placement;
  edges: CollapsedEdge[];
  layer: string;
  /** Non-null while a removal is being simulated. */
  impact: ImpactView | null;
  /** Changes only when the STRUCTURE does. Positions are rebuilt on it and on
   *  nothing else, which is what keeps a status poll from moving the picture. */
  layoutKey: string;
  selected: string | null;
  onSelect: (node: TopologyNode | null) => void;
}) {
  const showLoad = layer === 'power';
  const flowing = layer === 'cooling';

  const [nodes, setNodes, onNodesChange] = useNodesState<Node>(
    buildNodes(placement, showLoad, selected, impact));
  const [rfEdges, setEdges, onEdgesChange] = useEdgesState<Edge>(
    buildEdges(edges, flowing));

  const { fitView } = useReactFlow();
  const lastLayout = useRef(layoutKey);
  const [showMap, setShowMap] = useState(false);

  useEffect(() => {
    if (lastLayout.current !== layoutKey) {
      lastLayout.current = layoutKey;
      setNodes(buildNodes(placement, showLoad, selected, impact));
      setEdges(buildEdges(edges, flowing));
      // One frame for the new nodes to measure, then frame them.
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
    setEdges(buildEdges(edges, flowing));
    return undefined;
  }, [layoutKey, placement, edges, showLoad, flowing, selected, impact,
      setNodes, setEdges, fitView]);

  const onNodeClick = useCallback((_: unknown, n: Node) => {
    onSelect((n.data as { node: TopologyNode }).node);
  }, [onSelect]);

  const canMap = placement.placed.length > 25;

  // The flow animation is a per-frame stroke repaint, and it fights the
  // viewport transform while someone is panning or zooming - the whole canvas
  // judders. Pause it by toggling a class directly rather than through state,
  // so a pan does not re-render every node on the way past. Not keyed off a
  // React Flow class name: those are the library's internals to rename.
  const wrap = useRef<HTMLDivElement>(null);
  const idle = useRef<number | undefined>(undefined);
  const onMoveStart = useCallback(() => {
    if (idle.current) window.clearTimeout(idle.current);
    wrap.current?.classList.add('is-moving');
  }, []);
  const onMoveEnd = useCallback(() => {
    // Debounced: a wheel zoom fires many start/end pairs in quick succession.
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
    </div>
  );
}

export function Canvas(props: Parameters<typeof Flow>[0]) {
  // One provider per canvas. The maximized copy is a second, independent
  // instance with its own viewport, which is what lets someone zoom into the
  // big one without dragging the small one around underneath it.
  return (
    <ReactFlowProvider>
      <Flow {...props} />
    </ReactFlowProvider>
  );
}

/** The key the canvas rebuilds positions on. Exported so the page and the
 *  layout memo cannot drift apart about what counts as a change. */
export function useLayoutKey(layer: string, structure: string): string {
  return useMemo(() => `${layer}:${structure}`, [layer, structure]);
}
