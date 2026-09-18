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
import RoomNode from './RoomNode';
import { NODE_H, NODE_W, type CollapsedEdge, type Placed, type RoomBox }
  from './layout';
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

/** The site view is read as REGIONS first - which room is where, what crosses
 *  between them - and it does not fit in a window at a zoom where a device
 *  name is still a word. The room rectangles are legible long after the names
 *  stop being, so this one is allowed to zoom out to the whole estate. Capped
 *  at 18% so it opens with a margin round the estate rather than filling the
 *  window edge to edge. */
const FIT_SITE = { padding: 0.06, minZoom: 0.06, maxZoom: 0.18, duration: 320 } as const;

const nodeTypes = { device: DeviceNode, room: RoomNode };
const edgeTypes = { link: LinkEdge };

export interface Placement {
  placed: Placed[];
  width: number;
  height: number;
  /** Only the site view has these: one rectangle per room, drawn behind the
   *  devices standing in it. */
  rooms?: RoomBox[];
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

/** Which room box a device stands in, by the name the layout grouped on. */
function roomIdOf(node: TopologyNode): string {
  return `room:${node.location.room_name || 'Unplaced'}`;
}

function buildNodes(placement: Placement, showLoad: boolean,
                    selected: string | null, impact: ImpactView | null,
                    showRooms: boolean): Node[] {
  // Rooms first and with a zIndex of their own: React Flow paints in array
  // order, a parent must be declared before its children, and a room drawn
  // after its devices would cover them.
  const boxes = placement.rooms ?? [];
  const rooms: Node[] = boxes.map((r) => ({
    id: `room:${r.id}`,
    type: 'room',
    position: { x: r.x - 14, y: r.y - 14 },
    data: { name: r.name, count: r.count, width: r.width + 28 },
    width: r.width + 28,
    height: r.height + 28,
    // A room is dragged as a REGION - the devices in it come along. Selectable
    // so that picking it up also puts the resize grips on it; the selection
    // means "this room is what I am handling", not "this is the device I am
    // reading", which is why it leaves the drawer alone.
    draggable: true,
    selectable: true,
    // By the label, not by the whole rectangle. A room covers a region of
    // canvas the operator still needs to pan across, and a drag surface that
    // size would swallow every pan that started inside a hall.
    dragHandle: '.cn-room-name',
    connectable: false,
    hidden: !showRooms,
    zIndex: 0,
  }));

  return rooms.concat(placement.placed.map((p) => {
    // Which room it stands in, carried on the node so a room drag knows what
    // to take with it. Deliberately NOT React Flow's own parenting: a child's
    // position is relative to its parent, which turns every other thing this
    // canvas does with coordinates - the layout, the focus fit, a dragged
    // node - into two cases.
    const roomId = roomIdOf(p.node);
    return {
      id: p.node.id,
      type: 'device',
      position: { x: p.x, y: p.y },
      data: { ...nodeData(p.node, showLoad, impact), roomId },
      selected: p.node.id === selected,
      // Dragging rearranges the picture, which is welcome; connecting two
      // nodes by hand would invent a cable, which is not.
      connectable: false,
      width: NODE_W,
      height: NODE_H,
      zIndex: 1,
    };
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
  rooms: 'M3 4h8v6H3zM13 4h8v6h-8zM3 14h8v6H3zM13 14h8v6h-8z',
  reset: 'M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8M3 3v5h5'
       + 'M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16M16 16h5v5',
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

function Toolbar({ onFit, onReset, showMap, onToggleMap, canMap,
                  showRooms, onToggleRooms, canRooms }: {
  onFit: () => void;
  onReset: () => void;
  showMap: boolean;
  onToggleMap: () => void;
  canMap: boolean;
  showRooms: boolean;
  onToggleRooms: () => void;
  /** Only the site view has rooms to show, so only it gets the button. */
  canRooms: boolean;
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
      {/* Reset, not fit: the percentage above already fits, and a control that
          only reframes is a second button for something already one click
          away. This one puts every box back where the layout put it - rooms
          that were dragged, boxes that were resized, a device somebody moved -
          and frames the result, because a reset nobody can see has not
          obviously happened. */}
      <button type="button" title="Reset layout" onClick={onReset}>
        <Icon d={PATH.reset} />
      </button>
      {canRooms && (
        <button type="button" aria-pressed={showRooms}
                title={showRooms ? 'Hide room outlines' : 'Show room outlines'}
                className={showRooms ? 'is-on' : undefined} onClick={onToggleRooms}>
          <Icon d={PATH.rooms} />
        </button>
      )}
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
                focusNonce, children }: {
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
  /** Bumped when something OFF the canvas picks a node - the device list, a
   *  finding in the audit. The canvas then brings it into view, because a
   *  selection you cannot see is not a selection. */
  focusNonce: number;
  /** The floating chrome. Rendered inside the canvas so it sits over the
   *  graph, and after ReactFlow so it stacks above without a z-index war. */
  children?: React.ReactNode;
}) {
  const showLoad = layer === 'power';
  const flowing = layer === 'cooling';

  // Outlines on by default where there are any: on the site view they are the
  // only thing that says where one room ends and the next begins.
  const [showRooms, setShowRooms] = useState(true);

  const [nodes, setNodes, onNodesChange] = useNodesState<Node>(
    buildNodes(placement, showLoad, selected, impact, showRooms));
  const [rfEdges, setEdges, onEdgesChange] = useEdgesState<Edge>(
    buildEdges(edges, layer, flowing));

  const { fitView, getNodes } = useReactFlow();
  const fitOpts = placement.rooms?.length ? FIT_SITE : FIT;
  const lastLayout = useRef(layoutKey);
  const [showMap, setShowMap] = useState(false);

  useEffect(() => {
    if (lastLayout.current !== layoutKey) {
      lastLayout.current = layoutKey;
      setNodes(buildNodes(placement, showLoad, selected, impact, showRooms));
      setEdges(buildEdges(edges, layer, flowing));
      const t = window.setTimeout(() => fitView(fitOpts), 30);
      return () => window.clearTimeout(t);
    }
    // Same structure, newer state: replace the data, keep every position -
    // including one the operator dragged.
    const byId = new Map(placement.placed.map((p) => [p.node.id, p.node]));
    setNodes((nds) => nds.map((n) => {
      const fresh = byId.get(n.id);
      // `roomId` is carried across, not recomputed: it says which room box
      // owns this device, and nodeData knows nothing about rooms. Dropping it
      // here is what made a room drag stop taking its devices after the first
      // poll - fifteen seconds in, every device silently lost its room and a
      // drag had nothing to move.
      return fresh
        ? { ...n,
            data: { ...nodeData(fresh, showLoad, impact),
                    roomId: (n.data as { roomId?: string }).roomId },
            selected: n.id === selected }
        : n;
    }));
    setEdges(buildEdges(edges, layer, flowing));
    return undefined;
    // showRooms is deliberately not a dependency: the toggle re-parents in
    // place, and rebuilding here would undo every room that had been moved.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layoutKey, placement, edges, layer, showLoad, flowing, selected, impact,
      setNodes, setEdges, fitView, fitOpts]);

  const onNodeClick = useCallback((_: unknown, n: Node) => {
    const d = n.data as { node?: TopologyNode };
    if (d.node) onSelect(d.node);
  }, [onSelect]);

  /** Show or hide the outlines. Nothing else moves: the devices were never
   *  inside the boxes as far as the canvas is concerned, so the rectangles
   *  come and go on their own. */
  const onToggleRooms = useCallback(() => {
    setShowRooms((on) => {
      setNodes((nds) => nds.map(
        (n) => (n.type === 'room' ? { ...n, hidden: on } : n)));
      return !on;
    });
  }, [setNodes]);

  /** Dragging a room drags the room's devices.
   *
   *  A room is a region: moving the outline and leaving its contents behind
   *  would be a lie about what the outline means.
   *
   *  Absolute, not incremental. The first version added each drag event's
   *  delta to wherever the devices currently were, which is only correct if
   *  every event arrives exactly once - and they do not. A batch that also
   *  carried a measurement was skipped, a rebuilt box could seed from a stale
   *  position, and the devices ended up somewhere between where they started
   *  and where the outline went, or nowhere at all. Here the members are
   *  snapshotted when the room is picked up and re-placed from that snapshot
   *  on every event, so a dropped or repeated event changes nothing.
   */
  const dragging = useRef<{
    id: string;
    from: { x: number; y: number };
    members: { id: string; x: number; y: number }[];
  } | null>(null);

  const onNodeDragStart = useCallback((_: unknown, n: Node) => {
    if (n.type !== 'room') { dragging.current = null; return; }
    dragging.current = {
      id: n.id,
      from: { x: n.position.x, y: n.position.y },
      members: getNodes()
        .filter((d) => (d.data as { roomId?: string }).roomId === n.id)
        .map((d) => ({ id: d.id, x: d.position.x, y: d.position.y })),
    };
  }, [getNodes]);

  const onNodeDrag = useCallback((_: unknown, n: Node) => {
    const grip = dragging.current;
    if (!grip || grip.id !== n.id) return;
    const dx = n.position.x - grip.from.x;
    const dy = n.position.y - grip.from.y;
    const at = new Map(grip.members.map((m) => [m.id, m]));
    setNodes((nds) => nds.map((d) => {
      const m = at.get(d.id);
      return m ? { ...d, position: { x: m.x + dx, y: m.y + dy } } : d;
    }));
  }, [setNodes]);

  const onNodeDragStop = useCallback((e: unknown, n: Node) => {
    onNodeDrag(e, n);          // the last position, in case it never arrived
    dragging.current = null;
  }, [onNodeDrag]);

  // Bring an off-canvas selection into view, and only then: clicking a node
  // that is already on screen must not yank the viewport out from under the
  // hand that clicked it, which is why this keys off a nonce the list bumps
  // rather than off `selected`.
  const lastFocus = useRef(focusNonce);
  useEffect(() => {
    if (focusNonce === lastFocus.current || !selected) return;
    lastFocus.current = focusNonce;
    fitView({ nodes: [{ id: selected }], padding: 2.2, maxZoom: 1.1, duration: 320 });
  }, [focusNonce, selected, fitView]);

  /** Back to the drawn layout: positions, room boxes, sizes and all, then
   *  framed. Everything here is a pure function of the graph, so this is a
   *  rebuild rather than an undo stack. */
  const onReset = useCallback(() => {
    setNodes(buildNodes(placement, showLoad, selected, impact, showRooms));
    setEdges(buildEdges(edges, layer, flowing));
    window.setTimeout(() => fitView(fitOpts), 30);
  }, [placement, showLoad, selected, impact, showRooms, edges, layer, flowing,
      setNodes, setEdges, fitView, fitOpts]);

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
        onNodeDragStart={onNodeDragStart}
        onNodeDrag={onNodeDrag}
        onNodeDragStop={onNodeDragStop}
        onPaneClick={() => onSelect(null)}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        nodesConnectable={false}
        elementsSelectable
        fitView
        fitViewOptions={fitOpts}
        minZoom={0.05}
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

      <Toolbar onFit={() => fitView(fitOpts)} onReset={onReset}
               showMap={showMap} canMap={canMap}
               onToggleMap={() => setShowMap((v) => !v)}
               showRooms={showRooms} canRooms={Boolean(placement.rooms?.length)}
               onToggleRooms={onToggleRooms} />

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
