import type { Spin } from './ViewBar';
import {
  Html, MapControls, OrbitControls, OrthographicCamera, PerspectiveCamera, PointerLockControls,
} from '@react-three/drei';
import { useFrame, useThree, type ThreeEvent } from '@react-three/fiber';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, type ElementRef } from 'react';
import * as THREE from 'three';
import type {
  FloorAisle, FloorEquipment, FloorRack, ThermalField, ThermalUnit, TwinDevice, TwinRackIndex, TwinRoomIndices,
  TwinRoomScene,
} from '../../api/client';
import { humanise } from '../../lib/format';
import { alarmColor, resolveColor } from './colors';
import {
  bandColor, paintEquipment, paintRack, rackTiers, unitReadings, ventTiles, HEAT_INDEX, RACK_BASE, TEMP_BANDS, U, TILE,
  type CoolingLayer, type HeatPlane, type RackLayer, type RackPaint, type Sel, type ViewMode, type Visibility,
} from './layers';
import { OpenEnclosure, RackDetails, RoomFittings, UnitDetails, type RoomDoor, type RoomFire } from './Fittings';
import { Hardware } from './Hardware';
import { DeckEdge, Plenum, PLENUM_H } from './Underfloor';
import type { PathOverlay, Role } from './paths';
import { fmtT, numT, unitLabel } from './units';

/**
 * The room in WebGL (docs/27 Phase 1, restyled after the reference viewer):
 * a white room with its tile grid, perforated vent tiles in the cold aisles,
 * racks as cabinets colour-graded from floor to top by the air at each height,
 * air handlers graded from their discharge to their return, and every reading
 * in a legend band rather than a ramp.
 *
 * Three ways of looking at it on one scene: orbit (3D), a true top-down
 * orthographic PLAN, and a first-person walk (FPV) down the aisle with a
 * reticle that reads whichever rack it rests on.
 *
 * Cost: racks and units are instanced meshes with a per-instance gradient in
 * the shader (three colour attributes), so a hall is a handful of draw calls;
 * labels are sprites drawn on a canvas, so nothing fetches a font from a CDN.
 */

const WALL_H = 3.0;
const DEFAULT_W = 0.6, DEFAULT_D = 1.2;
const NO_BOXES: GBox[] = [];
const CAPACITY_LAYERS = new Set<RackLayer>(['power', 'utilisation', 'committed', 'space']);
const EYE = 1.6;

// The physical surfaces - concrete, steel, tile - are the same under both
// themes, like the readings' bands; only the chrome around the picture follows
// the theme. These are a rendering of the room, not UI colour.
const SURF = {
  floor: '#f7f8f9', tileLine: '#aab0b6', slab: '#dfe2e6', slabLine: '#cdd1d6',
  ventBg: '#5b6067', ventDot: '#c3c8cd', ventFrame: '#d7dbdf', wall: '#d5d8dc', wallSide: '#c7cacf', wallEdge: '#3b3e43',
  rackEdge: '#23272e', front: '#5b6470', instrument: '#4d8fa6',
  /* the room stands on a concrete apron in a slate studio, darkening toward
     the bottom; the light theme's is lighter, but always darker than the floor */
  backdrop: ['#3a4048', '#1a1d22'], backdropLight: ['#8a9097', '#5e6266'], apron: '#c6c9ce',
  coldAisle: '#2f7fd1', hotAisle: '#d64541',
  /* the frosted headroom above a capacity gauge */
  frost: '#dde5ee',
};

export interface HoverTip { x: number; y: number; text: string }
export interface SceneProps {
  data: TwinRoomScene;
  units: Map<string, ThermalUnit>;
  rackLayer: RackLayer;
  coolingLayer: CoolingLayer;
  vis: Visibility;
  mode: ViewMode;
  sel: Sel;
  /** A power/cooling path or an impact answer to draw over the room. */
  overlay?: PathOverlay | null;
  /** The room's indices: hot-spot markers are drawn from them. */
  indices?: TwinRoomIndices | null;
  /** The interpolated air, and which of its planes to show. */
  field?: ThermalField | null;
  heat: HeatPlane;
  /** The room's door openings, from the building fabric and its door contacts. */
  doors?: RoomDoor[];
  /** What the room's fire instruments read; null when it has none. */
  fire?: RoomFire | null;
  resetTick: number;
  /** A quarter turn of the orbit camera, counted so each press lands. */
  spin?: Spin;
  themeTick: number;
  onSelect: (s: Sel) => void;
  onTip: (t: HoverTip | null) => void;
  /** FPV only: the rack under the reticle, or null. */
  onFocus: (rackId: string | null) => void;
}

export function rackH(r: FloorRack): number {
  return RACK_BASE + (r.u_height ?? 42) * U;
}

// ----------------------------------------------------------- gradient boxes

// Each instance carries a bottom/middle/top colour for its front and, when a
// layer reads the two faces apart (inlet / outlet), another set for its rear;
// aFront says which local z sign is the front (0 = one gradient all round).
const GRADIENT_VERT = /* glsl */`
  attribute vec3 cBot; attribute vec3 cMid; attribute vec3 cTop;
  attribute vec3 rBot; attribute vec3 rMid; attribute vec3 rTop; attribute float aFront;
  varying vec3 vBot; varying vec3 vMid; varying vec3 vTop;
  varying vec3 vRBot; varying vec3 vRMid; varying vec3 vRTop;
  varying float vT; varying float vZ; varying float vSplit; varying vec3 vN;
  varying vec3 vP; varying vec3 vLN;
  void main() {
    vP = position; vLN = normal;
    vBot = cBot; vMid = cMid; vTop = cTop;
    vRBot = rBot; vRMid = rMid; vRTop = rTop;
    vT = position.y + 0.5;
    vZ = position.z * aFront;
    vSplit = abs(aFront);
    vN = normalize((modelMatrix * instanceMatrix * vec4(normal, 0.0)).xyz);
    gl_Position = projectionMatrix * modelViewMatrix * instanceMatrix * vec4(position, 1.0);
  }`;
const GRADIENT_FRAG = /* glsl */`
  uniform vec3 uLight; uniform float uOpacity; uniform float uFrost;
  varying vec3 vBot; varying vec3 vMid; varying vec3 vTop;
  varying vec3 vRBot; varying vec3 vRMid; varying vec3 vRTop;
  varying float vT; varying float vZ; varying float vSplit; varying vec3 vN;
  varying vec3 vP; varying vec3 vLN;
  void main() {
    bool rear = vSplit > 0.5 && vZ < 0.0;
    vec3 b = rear ? vRBot : vBot; vec3 m = rear ? vRMid : vMid; vec3 t = rear ? vRTop : vTop;
    vec3 c = vT < 0.5 ? mix(b, m, vT * 2.0) : mix(m, t, (vT - 0.5) * 2.0);
    float l = 0.64 + 0.36 * max(dot(normalize(vN), normalize(uLight)), 0.0);
    float a = uOpacity;
    if (uFrost > 0.5) {
      // Frosted glass: flatter, milkier light, and each face brightens and
      // thickens toward its rim so the empty share still reads as a cabinet.
      vec3 n = abs(vLN);
      vec2 q = n.x > 0.5 ? vP.yz : n.y > 0.5 ? vP.xz : vP.xy;
      float e = smoothstep(0.8, 1.0, max(abs(q.x), abs(q.y)) * 2.0);
      l = mix(l, 1.0, 0.5);
      c = mix(c, vec3(1.0), 0.35 * e);
      a = min(1.0, a + 0.22 * e);
    }
    gl_FragColor = vec4(c * l, a);
    #include <colorspace_fragment>
  }`;

export interface GBox {
  x: number; y: number; z: number;      // centre of the FULL box
  w: number; h: number; d: number;
  rot: number;                          // radians about +y
  /** Which local z sign the front face is on: -1 (faces lower y), +1, or 0 unknown. */
  front: number;
  paint: RackPaint;
}

/** One instanced mesh of boxes, each with its own bottom/middle/top colour.
 *  `part` 'solid' draws the filled share of each box, 'shell' the translucent
 *  remainder above it (only for boxes that are not full). */
function GradientBoxes({ boxes, part, opacity, frost = false, dim, meshRef, onClick, onMove, onOut }: {
  boxes: GBox[]; part: 'solid' | 'shell'; opacity: number;
  /** Draw as frosted glass (the headroom above a capacity gauge). */
  frost?: boolean;
  /** Boxes not part of the active overlay fade toward the walls. */
  dim?: boolean[];
  meshRef?: React.MutableRefObject<THREE.InstancedMesh | null>;
  onClick?: (i: number) => void; onMove?: (i: number, e: ThreeEvent<PointerEvent>) => void; onOut?: () => void;
}) {
  const inner = useRef<THREE.InstancedMesh>(null);
  const { invalidate } = useThree();
  const n = boxes.length;

  const geometry = useMemo(() => {
    const g = new THREE.BoxGeometry(1, 1, 1);
    for (const name of ['cBot', 'cMid', 'cTop', 'rBot', 'rMid', 'rTop']) {
      g.setAttribute(name, new THREE.InstancedBufferAttribute(new Float32Array(Math.max(1, n) * 3), 3));
    }
    g.setAttribute('aFront', new THREE.InstancedBufferAttribute(new Float32Array(Math.max(1, n)), 1));
    return g;
  }, [n]);
  const material = useMemo(() => new THREE.ShaderMaterial({
    vertexShader: GRADIENT_VERT, fragmentShader: GRADIENT_FRAG,
    uniforms: { uLight: { value: new THREE.Vector3(0.45, 1, 0.55) }, uOpacity: { value: opacity }, uFrost: { value: frost ? 1 : 0 } },
    transparent: opacity < 1, depthWrite: opacity >= 1,
  }), []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    material.uniforms.uOpacity.value = opacity;
    material.uniforms.uFrost.value = frost ? 1 : 0;
    material.transparent = opacity < 1;
    material.depthWrite = opacity >= 1;
    material.needsUpdate = true;
  }, [material, opacity, frost]);
  useEffect(() => () => { geometry.dispose(); }, [geometry]);
  useEffect(() => () => { material.dispose(); }, [material]);

  useLayoutEffect(() => {
    const im = inner.current;
    if (!im) return;
    if (meshRef) meshRef.current = im;
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), p = new THREE.Vector3(), s = new THREE.Vector3();
    const up = new THREE.Vector3(0, 1, 0);
    const col = new THREE.Color(), wall = new THREE.Color(SURF.wall);
    const attr = (k: string) => geometry.getAttribute(k) as THREE.InstancedBufferAttribute;
    const aB = attr('cBot'), aM = attr('cMid'), aT = attr('cTop');
    const rB = attr('rBot'), rM = attr('rMid'), rT = attr('rTop'), aF = attr('aFront');
    boxes.forEach((b, i) => {
      const fill = Math.min(1, Math.max(0, b.paint.fill));
      let h: number, yc: number;
      if (part === 'solid') { h = b.h * fill; yc = b.y - b.h / 2 + h / 2; }
      else { h = fill < 1 ? b.h * (1 - fill) : 0; yc = b.y + b.h / 2 - h / 2; }
      q.setFromAxisAngle(up, b.rot);
      im.setMatrixAt(i, m.compose(p.set(b.x, yc, b.z), q, s.set(b.w, Math.max(h, 0), b.d)));
      const rear = b.paint.rear ?? b.paint;
      const split = part === 'solid' && b.paint.rear && b.front ? b.front : 0;
      for (const [a, c] of [[aB, b.paint.bot], [aM, b.paint.mid], [aT, b.paint.top],
                            [rB, rear.bot], [rM, rear.mid], [rT, rear.top]] as const) {
        col.set(part === 'shell' ? (frost ? SURF.frost : SURF.wall) : c);
        if (dim?.[i]) col.lerp(wall, 0.78);
        a.setXYZ(i, col.r, col.g, col.b);
      }
      aF.setX(i, split);
    });
    im.instanceMatrix.needsUpdate = true;
    aB.needsUpdate = aM.needsUpdate = aT.needsUpdate = true;
    rB.needsUpdate = rM.needsUpdate = rT.needsUpdate = aF.needsUpdate = true;
    im.computeBoundingSphere();
    invalidate();
  }, [boxes, part, frost, dim, geometry, invalidate, meshRef]);

  if (!n) return null;
  return (
    <instancedMesh key={`${part}${n}`} ref={inner} args={[geometry, material, n]}
                   frustumCulled={false}
                   onClick={onClick && ((e) => { e.stopPropagation(); if (e.instanceId != null) onClick(e.instanceId); })}
                   onPointerMove={onMove && ((e) => { e.stopPropagation(); if (e.instanceId != null) onMove(e.instanceId, e); })}
                   onPointerOut={onOut} />
  );
}

// ----------------------------------------------------------- textures

function canvasTexture(size: number, draw: (c: CanvasRenderingContext2D, s: number) => void,
                       repeat?: [number, number]): THREE.CanvasTexture {
  const cv = document.createElement('canvas');
  cv.width = cv.height = size;
  draw(cv.getContext('2d')!, size);
  const t = new THREE.CanvasTexture(cv);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 16;
  if (repeat) { t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(repeat[0], repeat[1]); }
  return t;
}

/** A deterministic scatter, so every tile mottles the same way on every load. */
function seeded(seed: number): () => number {
  let x = seed | 0 || 1;
  return () => { x ^= x << 13; x ^= x >>> 17; x ^= x << 5; return ((x >>> 0) % 10000) / 10000; };
}

/** One raised-floor tile: stone with a faint mottle, under a soft grid line.
 *  A plain white square read as paper; the mottle is what makes it a floor. */
function tileTexture(W: number, D: number, slab: boolean): THREE.CanvasTexture {
  return canvasTexture(256, (c, s) => {
    c.fillStyle = slab ? SURF.slab : SURF.floor; c.fillRect(0, 0, s, s);
    if (!slab) {
      const rnd = seeded(7);
      for (let k = 0; k < 90; k++) {
        const x = rnd() * s, y = rnd() * s, r = 6 + rnd() * 26;
        const g = c.createRadialGradient(x, y, 0, x, y, r);
        const a = 0.025 + rnd() * 0.045;
        g.addColorStop(0, `rgba(150,156,164,${a})`); g.addColorStop(1, 'rgba(150,156,164,0)');
        c.fillStyle = g; c.beginPath(); c.arc(x, y, r, 0, Math.PI * 2); c.fill();
      }
      for (let k = 0; k < 400; k++) {
        c.fillStyle = `rgba(120,126,134,${0.03 + rnd() * 0.05})`;
        c.fillRect(rnd() * s, rnd() * s, 1.5, 1.5);
      }
    }
    if (slab) { c.strokeStyle = SURF.slabLine; c.lineWidth = 1.5; c.strokeRect(0.5, 0.5, s - 1, s - 1); }
  }, [W / (slab ? 1.0 : TILE), D / (slab ? 1.0 : TILE)]);
}

/** The tile grid as lines, not texture: a line stays one pixel wide from any
 *  distance, where a line painted into the tile melted into the white as the
 *  floor shrank on screen. */
function tileGrid(W: number, D: number): THREE.BufferGeometry {
  const pts: number[] = [];
  const cols = Math.round(W / TILE), rows = Math.round(D / TILE);
  for (let i = 0; i <= cols; i++) { const x = -W / 2 + i * TILE; pts.push(x, 0, -D / 2, x, 0, D / 2); }
  for (let j = 0; j <= rows; j++) { const z = -D / 2 + j * TILE; pts.push(-W / 2, 0, z, W / 2, 0, z); }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
  return g;
}

/** A soft dark pool under a cabinet: the contact shadow that seats it on the floor. */
function shadowTexture(): THREE.CanvasTexture {
  return canvasTexture(128, (c, s) => {
    c.clearRect(0, 0, s, s);
    const g = c.createRadialGradient(s / 2, s / 2, s * 0.18, s / 2, s / 2, s * 0.5);
    g.addColorStop(0, 'rgba(20,24,30,0.55)'); g.addColorStop(0.55, 'rgba(20,24,30,0.28)'); g.addColorStop(1, 'rgba(20,24,30,0)');
    c.fillStyle = g; c.fillRect(0, 0, s, s);
  });
}

/** Contact shadows for every box on the floor, one instanced plane. */
function ContactShadows({ boxes }: { boxes: GBox[] }) {
  const ref = useRef<THREE.InstancedMesh>(null);
  const tex = useMemo(shadowTexture, []);
  useEffect(() => () => tex.dispose(), [tex]);
  useLayoutEffect(() => {
    const im = ref.current;
    if (!im) return;
    const m = new THREE.Matrix4(), p = new THREE.Vector3(), s = new THREE.Vector3();
    const flat = new THREE.Quaternion().setFromEuler(new THREE.Euler(-Math.PI / 2, 0, 0));
    boxes.forEach((b, i) => {
      const q = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), b.rot).multiply(flat);
      im.setMatrixAt(i, m.compose(p.set(b.x, 0.012, b.z), q, s.set(b.w * 1.9, b.d * 1.9, 1)));
    });
    im.instanceMatrix.needsUpdate = true;
    im.computeBoundingSphere();
  }, [boxes]);
  if (!boxes.length) return null;
  return (
    <instancedMesh key={boxes.length} ref={ref} args={[undefined, undefined, boxes.length]} raycast={() => null}>
      <planeGeometry args={[1, 1]} />
      <meshBasicMaterial map={tex} transparent depthWrite={false} />
    </instancedMesh>
  );
}

/** One cold-aisle tile: blue, with the dashed edge a marked-out tile wears. */
function aisleTexture(cols: number, rows: number): THREE.CanvasTexture {
  return canvasTexture(64, (c, s) => {
    c.fillStyle = SURF.coldAisle; c.fillRect(0, 0, s, s);
    c.strokeStyle = 'rgba(255,255,255,0.75)'; c.lineWidth = 2; c.setLineDash([6, 4]);
    c.strokeRect(2, 2, s - 4, s - 4);
  }, [Math.max(1, cols), Math.max(1, rows)]);
}

/** A perforated airflow tile: dark steel in a light frame, the holes a
 *  regular field of light dots - read from across the room as a vent. */
function ventTexture(): THREE.CanvasTexture {
  return canvasTexture(128, (c, s) => {
    c.fillStyle = SURF.ventFrame; c.fillRect(0, 0, s, s);
    c.fillStyle = SURF.ventBg; c.fillRect(5, 5, s - 10, s - 10);
    c.fillStyle = SURF.ventDot;
    for (let y = 13; y < s - 8; y += 9) for (let x = 13; x < s - 8; x += 9) {
      c.beginPath(); c.arc(x, y, 2.1, 0, Math.PI * 2); c.fill();
    }
  });
}

/** A strip of grid-reference labels, one per tile, as a single texture. */
function stripTexture(labels: string[], ink: string): THREE.CanvasTexture {
  const cell = 64;
  const cv = document.createElement('canvas');
  cv.width = Math.max(1, labels.length) * cell; cv.height = cell;
  const c = cv.getContext('2d')!;
  c.font = `600 ${cell * 0.42}px system-ui, sans-serif`;
  c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillStyle = ink;
  labels.forEach((l, i) => c.fillText(l, i * cell + cell / 2, cell / 2));
  const t = new THREE.CanvasTexture(cv);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 8;
  return t;
}

export function labelTexture(text: string, ink: string, bg: string): THREE.CanvasTexture {
  const w = 320, h = 80;
  const cv = document.createElement('canvas');
  cv.width = w; cv.height = h;
  const c = cv.getContext('2d')!;
  c.fillStyle = bg;
  c.beginPath(); c.roundRect(2, 2, w - 4, h - 4, 14); c.fill();
  c.font = `600 34px system-ui, sans-serif`;
  c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillStyle = ink;
  c.fillText(text.length > 18 ? text.slice(0, 17) + '…' : text, w / 2, h / 2 + 1);
  const t = new THREE.CanvasTexture(cv);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

// ----------------------------------------------------------- the scene

export default function RoomScene(p: SceneProps) {
  const { data, units, rackLayer, coolingLayer, vis, mode, sel, overlay, indices, field, heat, doors, fire, resetTick, spin, themeTick,
          onSelect, onTip, onFocus } = p;
  const { plan, devices } = data;
  const W = plan.extent.width_m, D = plan.extent.depth_m;
  const whiteSpace = plan.room_class === 'white_space';
  const X = useCallback((x: number) => x - W / 2, [W]);
  const Z = useCallback((y: number) => y - D / 2, [D]);
  const { invalidate } = useThree();

  // Chrome tokens, re-resolved on a theme change (themeTick).
  const tones = useMemo(() => ({
    bg: resolveColor('var(--bg-inset)'), accent: resolveColor('var(--accent)'),
    cold: resolveColor('var(--cat-cap)'), hot: resolveColor('var(--critical)'),
    ink: resolveColor('var(--text)'), inkMuted: resolveColor('var(--text-muted)'),
    raised: resolveColor('var(--bg-raised)'),
    power: resolveColor('var(--layer-power)'), cooling: resolveColor('var(--layer-cooling)'),
    warn: resolveColor('var(--warn)'), critical: resolveColor('var(--critical)'),
  }), [themeTick]);

  // --- data shaping -------------------------------------------------------
  const byRack = useMemo(() => {
    const m = new Map<string, TwinDevice[]>();
    for (const d of devices) {
      const arr = m.get(d.rack_id);
      if (arr) arr.push(d); else m.set(d.rack_id, [d]);
    }
    return m;
  }, [devices]);
  const rackById = useMemo(() => new Map(plan.racks.map((r) => [r.id, r])), [plan.racks]);

  const rackBoxes = useMemo<GBox[]>(() => {
    const peak = Math.max(0, ...plan.racks.map((r) => r.load_kw ?? 0));
    return plan.racks.map((r) => {
      const h = rackH(r);
      return { x: X(r.x), y: h / 2, z: Z(r.y), w: r.w_m ?? DEFAULT_W, h, d: r.d_m ?? DEFAULT_D, rot: 0,
               front: r.facing === 'N' ? -1 : r.facing === 'S' ? 1 : 0,
               paint: paintRack(r, byRack.get(r.id) ?? [], rackLayer, peak) };
    });
  }, [plan.racks, byRack, rackLayer, X, Z, themeTick]); // eslint-disable-line react-hooks/exhaustive-deps

  const placedUnits = useMemo(() => plan.equipment.filter((e) => e.w_m != null && e.d_m != null), [plan.equipment]);
  const instruments = useMemo(() => plan.equipment.filter((e) => e.w_m == null || e.d_m == null), [plan.equipment]);
  const unitBoxes = useMemo<GBox[]>(() => placedUnits.map((e) => {
    const h = e.h_m ?? 1.5;
    const y = e.mount === 'wall' ? (e.mount_height_m ?? h / 2) : h / 2;
    return { x: X(e.x as number), y, z: Z(e.y as number), w: e.w_m as number, h, d: e.d_m as number,
             rot: -((e.facing_deg ?? 0) * Math.PI) / 180, front: 0,
             paint: paintEquipment(e, coolingLayer, unitReadings(e, units.get(e.id))) };
  }), [placedUnits, units, coolingLayer, X, Z, themeTick]); // eslint-disable-line react-hooks/exhaustive-deps

  // A yard stands in the open and a roof behind its parapet; everything else has walls.
  const enclosure: 'walls' | 'fence' | 'parapet' = plan.room_class === 'outdoor' ? 'fence'
    : /^roof$/i.test(plan.level ?? '') || /^roof$/i.test(plan.room_name) ? 'parapet' : 'walls';
  // What already stands on the floor, room metres: the fittings keep off it.
  const standing = useMemo(() => [
    ...plan.racks.map((r) => {
      const w = (r.w_m ?? DEFAULT_W) / 2, d = (r.d_m ?? DEFAULT_D) / 2;
      return { x0: r.x - w, y0: r.y - d, x1: r.x + w, y1: r.y + d };
    }),
    ...placedUnits.map((e) => {
      const quarter = Math.round(((e.facing_deg ?? 0) % 180) / 90) === 1;
      const w = ((quarter ? e.d_m : e.w_m) as number) / 2, d = ((quarter ? e.w_m : e.d_m) as number) / 2;
      return { x0: (e.x as number) - w, y0: (e.y as number) - d, x1: (e.x as number) + w, y1: (e.y as number) + d };
    }),
  ], [plan.racks, placedUnits]);
  const unitTypes = useMemo(() => placedUnits.map((e) => e.device_type), [placedUnits]);
  // A door's alarm colour arrives as a token; three.js needs the colour it resolves to.
  const fitDoors = useMemo(() => (doors ?? []).map((d) => ({ ...d, alarm: d.alarm ? resolveColor(d.alarm) : null })),
    [doors, themeTick]); // eslint-disable-line react-hooks/exhaustive-deps

  const vents = useMemo(() => ventTiles(plan.racks, plan.aisles, whiteSpace), [plan.racks, plan.aisles, whiteSpace]);

  // --- tooltips -----------------------------------------------------------
  const tip = (e: ThreeEvent<PointerEvent>, text: string) =>
    onTip({ x: e.nativeEvent.clientX, y: e.nativeEvent.clientY, text });
  const hotByRack = useMemo(() => {
    const m = new Map<string, TwinRackIndex>();
    for (const x of indices?.racks ?? []) m.set(x.rack_id, x);
    return m;
  }, [indices]);
  const rackText = (r: FloorRack) => {
    const [b, m, t] = rackTiers(byRack.get(r.id) ?? [], r.u_height ?? 42);
    const f = (v: number | null) => numT(v);
    const ix = hotByRack.get(r.id);
    return [r.name, `${r.device_count} devices`,
      b != null || t != null ? `inlet ${f(b)} / ${f(m)} / ${f(t)} ${unitLabel()}` : 'no inlet reading',
      ix?.exhaust_max_c != null ? `exhaust ${fmtT(ix.exhaust_max_c)}` : null,
      ix?.hot ? (ix.hot === 'out' ? 'hot spot · out of allowable' : 'hot spot') : null,
      r.load_kw != null ? `${r.load_kw.toFixed(1)} kW` : null,
      r.free_u != null ? `${r.free_u} U free` : null].filter(Boolean).join(' · ');
  };
  const unitText = (e: FloorEquipment) => {
    const u = unitReadings(e, units.get(e.id));
    return [e.name, humanise(e.device_type),
      u.supply_c != null ? `supply ${fmtT(u.supply_c)}` : null,
      u.return_c != null ? `return ${fmtT(u.return_c)}` : null,
      e.power_w != null ? `${(e.power_w / 1000).toFixed(1)} kW` : null,
      e.max_severity !== 'CLEAR' ? e.max_severity.toLowerCase() : null].filter(Boolean).join(' · ');
  };

  const rackMesh = useRef<THREE.InstancedMesh | null>(null);
  const fpv = mode === 'fpv';
  const anyShell = rackBoxes.some((b) => b.paint.fill < 1);
  // Capacity layers draw each rack as a gauge: the colour rises to the value
  // and the headroom above it is frosted. Devices sit at their own U, which
  // has nothing to do with the gauge's height, so they stay hidden unless the
  // viewer asks to see into the racks. The plan looks straight down through
  // the headroom at the gauge's top, so it keeps the clear shell.
  // An opened raised floor: the tiles go almost clear, and the floor's overlays
  // (aisle tint, shadows, solid vents) step aside so the void reads from a low angle.
  const seeUnder = whiteSpace && vis.underfloor;
  const gauge = CAPACITY_LAYERS.has(rackLayer);
  const frost = gauge && !vis.faces && mode !== 'plan';

  // Which racks and units the overlay touches, so the rest can fade.
  const rackRole = useMemo(() => plan.racks.map((r) => {
    if (!overlay) return null;
    let best: Role | null = null;
    for (const d of byRack.get(r.id) ?? []) {
      const role = overlay.roles.get(d.id);
      if (role && (best == null || RANK[role] > RANK[best])) best = role;
    }
    return best;
  }), [plan.racks, byRack, overlay]);
  const unitRole = useMemo(() => placedUnits.map((e) => overlay?.roles.get(e.id) ?? null), [placedUnits, overlay]);
  const rackDim = useMemo(() => (overlay ? rackRole.map((x) => x == null) : undefined), [overlay, rackRole]);
  const unitDim = useMemo(() => (overlay ? unitRole.map((x) => x == null) : undefined), [overlay, unitRole]);

  return (
    <>
      <Backdrop themeTick={themeTick} />
      <hemisphereLight args={['#ffffff', '#b9bec6', 0.9]} />
      <directionalLight position={[W * 0.6, 14, D * 0.4]} intensity={0.55} />

      <Cameras mode={mode} W={W} D={D} resetTick={resetTick} spin={spin} aisles={plan.aisles} X={X} Z={Z} />
      {fpv && <FpvRig W={W} D={D} rackMesh={rackMesh} racks={plan.racks} onFocus={onFocus} />}

      {/* A white-space room stands on a raised floor: its deck is always drawn;
          "See under the raised floor" turns the tiles to glass over the void. */}
      <Room W={W} D={D} whiteSpace={whiteSpace} walls={enclosure === 'walls'} seeUnder={seeUnder} />
      {whiteSpace && !seeUnder && <DeckEdge W={W} D={D} />}
      {seeUnder && (
        <Plenum W={W} D={D} X={X} Z={Z} racks={plan.racks} aisles={plan.aisles} units={plan.equipment} />
      )}
      {enclosure !== 'walls' && <OpenEnclosure W={W} D={D} kind={enclosure} />}
      {vis.fittings && enclosure === 'walls' && (
        <RoomFittings W={W} D={D} roomClass={plan.room_class} roomName={plan.room_name}
                      doors={fitDoors} obstacles={standing} fire={fire ?? null} />
      )}
      <GridRefs W={W} D={D} ink={tones.inkMuted} />
      {vis.vents && vents.length > 0 && <Vents vents={vents} X={X} Z={Z} glass={seeUnder} />}
      {mode !== 'plan' && !seeUnder && <ContactShadows boxes={[...rackBoxes, ...unitBoxes.filter((b) => b.y <= b.h / 2 + 0.05)]} />}
      {vis.aisles && plan.aisles.map((a) => (
        <AisleBand key={`${a.label}-${a.y_start}`} a={a} W={W} Z={Z} X={X} racks={plan.racks}
                   containment={vis.containment} tint={!seeUnder} />
      ))}

      {/* See-into-racks: the layer's colour stays on the cabinet as a tinted
          shell and the devices inside show through it. */}
      <GradientBoxes boxes={rackBoxes} part="solid" opacity={vis.faces ? 0.32 : 1} meshRef={rackMesh} dim={rackDim}
                     // Seen into, the shell lets the pointer through to the devices.
                     onClick={fpv || vis.faces ? undefined : (i) => onSelect({ kind: 'rack', id: plan.racks[i].id })}
                     onMove={fpv || vis.faces ? undefined : (i, e) => tip(e, rackText(plan.racks[i]))}
                     onOut={() => onTip(null)} />
      {anyShell && <GradientBoxes boxes={rackBoxes} part="shell" opacity={frost ? 0.85 : 0.22} frost={frost} />}
      {anyShell && <FillLines boxes={rackBoxes} />}
      <RackFrames racks={plan.racks} X={X} Z={Z} />
      {vis.detail && <RackDetails boxes={rackBoxes} open={vis.faces} />}
      {/* Frame, plinth, handles, nozzles, stacks - all outside the coloured body. */}
      <Hardware racks={rackBoxes} units={vis.plant ? unitBoxes : NO_BOXES} unitTypes={unitTypes} open={vis.faces} />
      {((vis.devices && !gauge) || vis.faces) && (
        <RackDevices devices={devices} rackById={rackById} X={X} Z={Z} themeTick={themeTick}
                     onSelect={fpv ? undefined : onSelect} onTip={fpv ? undefined : onTip} />
      )}

      {vis.plant && (
        <>
          <GradientBoxes boxes={unitBoxes} part="solid" opacity={1} dim={unitDim}
                         onClick={fpv ? undefined : (i) => onSelect({ kind: 'equipment', id: placedUnits[i].id })}
                         onMove={fpv ? undefined : (i, e) => tip(e, unitText(placedUnits[i]))}
                         onOut={() => onTip(null)} />
          <UnitFrames boxes={unitBoxes} />
          {vis.detail && <UnitDetails boxes={unitBoxes} types={unitTypes} />}
          <Instruments items={instruments} X={X} Z={Z}
                       onSelect={fpv ? undefined : onSelect} onTip={fpv ? undefined : onTip} />
        </>
      )}

      {heat !== 'off' && field && (
        <HeatMap field={field} plane={HEAT_INDEX[heat]} W={W} D={D} themeTick={themeTick} />
      )}
      {vis.hotspots && indices && indices.hot_spots > 0 && (
        <HotSpots indices={indices} racks={plan.racks} rackBoxes={rackBoxes} tones={tones}
                  onSelect={fpv ? undefined : onSelect} onTip={fpv ? undefined : onTip} />
      )}
      {vis.labels && <Labels racks={plan.racks} units={placedUnits} X={X} Z={Z}
                             ink={tones.ink} bg={tones.raised} />}
      <Highlight sel={sel} rackBoxes={rackBoxes} racks={plan.racks} unitBoxes={unitBoxes} units={placedUnits}
                 devices={devices} rackById={rackById} X={X} Z={Z} color={tones.accent} />
      {overlay && (
        <Overlay overlay={overlay} rackBoxes={rackBoxes} racks={plan.racks} rackRole={rackRole}
                 unitBoxes={unitBoxes} units={placedUnits} unitRole={unitRole} devices={devices}
                 tones={tones} themeTick={themeTick} />
      )}
      <Invalidator deps={[rackLayer, coolingLayer, vis, sel, mode, overlay, heat, field, indices, doors, fire]} invalidate={invalidate} />
    </>
  );
}

type Tones = Record<'bg' | 'accent' | 'cold' | 'hot' | 'ink' | 'inkMuted' | 'raised' | 'power' | 'cooling'
  | 'warn' | 'critical', string>;
const RANK: Record<Role, number> = { path: 1, degraded: 2, cut_off: 3, anchor: 4 };

/** Box outline as line-segment vertices, in the box's rotation. */
function boxEdges(b: GBox, pad: number, out: number[]): void {
  const v = new THREE.Vector3(), q = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), b.rot);
  const c = (sx: number, sy: number, sz: number) => {
    v.set(sx * (b.w / 2 + pad), sy * (b.h / 2 + pad), sz * (b.d / 2 + pad)).applyQuaternion(q);
    return [v.x + b.x, v.y + b.y, v.z + b.z];
  };
  const P = [c(-1, -1, -1), c(1, -1, -1), c(1, -1, 1), c(-1, -1, 1), c(-1, 1, -1), c(1, 1, -1), c(1, 1, 1), c(-1, 1, 1)];
  for (const [a, bb] of [[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4], [0, 4], [1, 5], [2, 6], [3, 7]]) {
    out.push(...P[a], ...P[bb]);
  }
}

/** A path or impact answer drawn over the room: outlines on every rack and
 *  unit it names, in the role's colour, and each hop as a cable run arching
 *  between the two boxes it joins. Hops to equipment outside this room are
 *  not drawn - the panel lists them with the room they stand in. */
function Overlay({ overlay, rackBoxes, racks, rackRole, unitBoxes, units, unitRole, devices, tones, themeTick }: {
  overlay: PathOverlay; rackBoxes: GBox[]; racks: FloorRack[]; rackRole: (Role | null)[];
  unitBoxes: GBox[]; units: FloorEquipment[]; unitRole: (Role | null)[]; devices: TwinDevice[];
  tones: Tones; themeTick: number;
}) {
  const geoms = useMemo(() => {
    const byRole: Record<Role, number[]> = { anchor: [], path: [], cut_off: [], degraded: [] };
    rackBoxes.forEach((b, i) => { const r = rackRole[i]; if (r) boxEdges(b, 0.03, byRole[r]); });
    unitBoxes.forEach((b, i) => { const r = unitRole[i]; if (r) boxEdges(b, 0.03, byRole[r]); });
    // Where a device id sits: its rack's top, or its unit's top.
    const top = new Map<string, [number, number, number]>();
    racks.forEach((r, i) => {
      const b = rackBoxes[i];
      for (const d of devices) if (d.rack_id === r.id) top.set(d.id, [b.x, b.y + b.h / 2 + 0.05, b.z]);
    });
    units.forEach((e, i) => { const b = unitBoxes[i]; top.set(e.id, [b.x, b.y + b.h / 2 + 0.05, b.z]); });
    const solid: number[] = [], dashed: number[] = [];
    for (const h of overlay.hops) {
      const a = top.get(h.from), b = top.get(h.to);
      if (!a || !b) continue;
      const rise = Math.max(a[1], b[1]) + 0.6 + Math.hypot(a[0] - b[0], a[2] - b[2]) * 0.08;
      const m: [number, number, number] = [(a[0] + b[0]) / 2, rise, (a[2] + b[2]) / 2];
      const into = h.side === 'B' ? dashed : solid;
      into.push(...a, ...m, ...m, ...b);
    }
    const mk = (pts: number[]) => {
      const g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
      return g;
    };
    const roles = Object.fromEntries(
      (Object.keys(byRole) as Role[]).map((k) => [k, mk(byRole[k])])) as Record<Role, THREE.BufferGeometry>;
    return { roles, solid: mk(solid), dashed: mk(dashed) };
  }, [overlay, rackBoxes, rackRole, unitBoxes, unitRole, racks, units, devices]);
  useEffect(() => () => {
    Object.values(geoms.roles).forEach((g) => g.dispose());
    geoms.solid.dispose();
    geoms.dashed.dispose();
  }, [geoms]);
  const line = overlay.kind === 'cooling' ? tones.cooling : tones.power;
  const roleColor: Record<Role, string> = { anchor: tones.accent, path: line, cut_off: tones.critical, degraded: tones.warn };
  const dashedRef = useRef<THREE.LineSegments>(null);
  useLayoutEffect(() => { dashedRef.current?.computeLineDistances(); }, [geoms, themeTick]);
  return (
    <group>
      {(Object.keys(geoms.roles) as Role[]).map((r) => (
        <lineSegments key={r} geometry={geoms.roles[r]}>
          <lineBasicMaterial color={roleColor[r]} />
        </lineSegments>
      ))}
      <lineSegments geometry={geoms.solid}><lineBasicMaterial color={line} /></lineSegments>
      <lineSegments ref={dashedRef} geometry={geoms.dashed}>
        <lineDashedMaterial color={line} dashSize={0.18} gapSize={0.1} />
      </lineSegments>
    </group>
  );
}

/** One of the three interpolated planes as a texture on a sheet at its
 *  height: a texel per cell, the legend's band colour for the air there,
 *  and the cell's confidence as its opacity, so air no sensor reached fades
 *  out instead of being painted. Cold aisles come from intakes, hot aisles
 *  from exhausts (the backend never blends the two); between the rows the
 *  racks themselves hide the sheet. */
function HeatMap({ field, plane, W, D, themeTick }: {
  field: ThermalField; plane: number; W: number; D: number; themeTick: number;
}) {
  const texture = useMemo(() => {
    const pl = field.planes[plane];
    const { nx, ny } = field;
    const data = new Uint8Array(nx * ny * 4);
    const col = new THREE.Color();
    const cache = new Map<string, [number, number, number]>();
    if (pl) {
      for (let j = 0; j < ny; j++) {
        for (let i = 0; i < nx; i++) {
          const k = j * nx + i;
          const t = pl.temp[k];
          if (t == null) continue;
          const token = bandColor(TEMP_BANDS, t);
          let rgb = cache.get(token);
          if (!rgb) {
            col.set(resolveColor(token));
            rgb = [Math.round(col.r * 255), Math.round(col.g * 255), Math.round(col.b * 255)];
            cache.set(token, rgb);
          }
          // Room y grows away from the top wall; the sheet's v grows the other
          // way once it lies flat, so rows are written bottom-up.
          const o = ((ny - 1 - j) * nx + i) * 4;
          data[o] = rgb[0]; data[o + 1] = rgb[1]; data[o + 2] = rgb[2];
          data[o + 3] = Math.round(Math.min(1, pl.conf[k]) * 0.88 * 255);
        }
      }
    }
    const tx = new THREE.DataTexture(data, nx, ny, THREE.RGBAFormat);
    tx.colorSpace = THREE.SRGBColorSpace;
    tx.magFilter = THREE.LinearFilter;
    tx.minFilter = THREE.LinearFilter;
    tx.needsUpdate = true;
    return tx;
  }, [field, plane, themeTick]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => texture.dispose(), [texture]);
  const h = field.planes[plane]?.height_m ?? 1.2;
  // The texture covers nx*cell by ny*cell, which may overshoot the room by a
  // part cell; the sheet is that size and centred on the room's origin corner.
  const sw = field.nx * field.cell_m, sd = field.ny * field.cell_m;
  return (
    <mesh rotation-x={-Math.PI / 2} position={[sw / 2 - W / 2, h, sd / 2 - D / 2]} renderOrder={2}>
      <planeGeometry args={[sw, sd]} />
      <meshBasicMaterial map={texture} transparent depthWrite={false} side={THREE.DoubleSide} />
    </mesh>
  );
}

/** A marker over every rack whose intake is past the recommended ceiling:
 *  amber inside the allowable band, red outside it. The rack's own colour
 *  already says so on the temperature layers; the marker says it on every
 *  other layer and from across the hall. */
function HotSpots({ indices, racks, rackBoxes, tones, onSelect, onTip }: {
  indices: TwinRoomIndices; racks: FloorRack[]; rackBoxes: GBox[]; tones: Tones;
  onSelect?: (s: Sel) => void; onTip?: (t: HoverTip | null) => void;
}) {
  const ref = useRef<THREE.InstancedMesh>(null);
  const { invalidate } = useThree();
  const spots = useMemo(() => {
    const at = new Map(racks.map((r, i) => [r.id, i]));
    return indices.racks.filter((x) => x.hot && at.has(x.rack_id)).map((x) => ({ ix: x, box: rackBoxes[at.get(x.rack_id)!], rack: racks[at.get(x.rack_id)!] }));
  }, [indices, racks, rackBoxes]);
  const colors = useMemo(() => spots.map((s) => new THREE.Color(s.ix.hot === 'out' ? tones.critical : tones.warn)), [spots, tones]);
  useLayoutEffect(() => {
    const im = ref.current;
    if (!im) return;
    const m = new THREE.Matrix4(), p = new THREE.Vector3(), s = new THREE.Vector3(1, 1, 1);
    const q = new THREE.Quaternion().setFromEuler(new THREE.Euler(Math.PI, 0, 0));   // tip down
    spots.forEach((sp, i) => {
      im.setMatrixAt(i, m.compose(p.set(sp.box.x, sp.box.y + sp.box.h / 2 + 0.36, sp.box.z), q, s));
      im.setColorAt(i, colors[i]);
    });
    im.instanceMatrix.needsUpdate = true;
    if (im.instanceColor) im.instanceColor.needsUpdate = true;
    im.computeBoundingSphere();
    invalidate();
  }, [spots, colors, invalidate]);
  if (!spots.length) return null;
  return (
    <instancedMesh key={spots.length} ref={ref} args={[undefined, undefined, spots.length]}
                   onClick={onSelect && ((e) => { e.stopPropagation(); if (e.instanceId != null) onSelect({ kind: 'rack', id: spots[e.instanceId].rack.id }); })}
                   onPointerMove={onTip && ((e) => {
                     e.stopPropagation();
                     if (e.instanceId == null) return;
                     const sp = spots[e.instanceId];
                     onTip({ x: e.nativeEvent.clientX, y: e.nativeEvent.clientY,
                             text: [sp.rack.name, sp.ix.hot === 'out' ? 'hot spot · out of allowable' : 'hot spot · above recommended',
                                    sp.ix.inlet_max_c != null ? `intake ${fmtT(sp.ix.inlet_max_c)}` : null].filter(Boolean).join(' · ') });
                   })}
                   onPointerOut={() => onTip?.(null)}>
      <coneGeometry args={[0.17, 0.4, 14]} />
      {/* Unlit: a marker is a flag, not a surface, and must read as the same
          amber or red from every angle. */}
      <meshBasicMaterial />
    </instancedMesh>
  );
}

function Invalidator({ deps, invalidate }: { deps: unknown[]; invalidate: () => void }) {
  useEffect(() => { invalidate(); }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

// ----------------------------------------------------------- cameras

function Cameras({ mode, W, D, resetTick, spin, aisles, X, Z }: {
  mode: ViewMode; W: number; D: number; resetTick: number; spin?: Spin; aisles: FloorAisle[];
  X: (x: number) => number; Z: (y: number) => number;
}) {
  const orbit = useRef<ElementRef<typeof OrbitControls>>(null);
  const map = useRef<ElementRef<typeof MapControls>>(null);
  const { camera, size, invalidate } = useThree();
  const span = Math.max(W, D);
  useQuarterTurn(spin, orbit, camera, invalidate);
  const dolly = useDolly(orbit, camera, invalidate);

  useEffect(() => {
    if (mode === '3d') {
      orbit.current?.target.set(0, 0.8, 0);
      dolly(new THREE.Vector3(span * 0.7, span * 0.75, span * 0.95), new THREE.Vector3(0, 0.8, 0));
      orbit.current?.update();
    } else if (mode === 'plan') {
      camera.position.set(0, 60, 0);
      camera.up.set(0, 0, -1);
      camera.lookAt(0, 0, 0);
      const cam = camera as THREE.OrthographicCamera;
      if (cam.isOrthographicCamera) {
        cam.zoom = Math.min(size.width / (W + 2.4), size.height / (D + 2.4));
        cam.updateProjectionMatrix();
      }
      map.current?.target.set(0, 0, 0);
      map.current?.update();
    } else {
      const cold = aisles.find((a) => a.kind === 'cold');
      const z = cold ? Z((cold.y_start + cold.y_end) / 2) : 0;
      camera.up.set(0, 1, 0);
      camera.position.set(X(0.6), EYE, z);
      camera.lookAt(X(W), EYE, z);
    }
    invalidate();
  }, [mode, resetTick, camera, span, W, D, size.width, size.height, aisles, X, Z, invalidate, dolly]);

  if (mode === 'plan') {
    return (
      <>
        <OrthographicCamera makeDefault near={0.1} far={500} position={[0, 60, 0]} />
        <MapControls ref={map} makeDefault enableRotate={false} screenSpacePanning
                     minZoom={4} maxZoom={400} />
      </>
    );
  }
  if (mode === 'fpv') {
    return <PerspectiveCamera makeDefault fov={70} near={0.05} far={300} />;
  }
  return (
    <>
      <PerspectiveCamera makeDefault fov={45} near={0.1} far={500} />
      <OrbitControls ref={orbit} makeDefault enableDamping={false}
                     maxPolarAngle={Math.PI / 2.05} minDistance={1.2} maxDistance={span * 4} />
    </>
  );
}

const DOLLY_MS = 300;

/** Arrive at a 3D home position with a short dolly in from a third further
 *  out, so entering a room or resetting the camera is a move, not a cut.
 *  Reduced motion, or no orbit yet, lands at once. */
export function useDolly(orbit: React.RefObject<{ update: () => void } | null>, camera: THREE.Camera, invalidate: () => void) {
  const run = useRef<{ from: THREE.Vector3; to: THREE.Vector3; t0: number } | null>(null);
  useFrame(() => {
    const d = run.current;
    if (!d) return;
    const k = Math.min(1, (performance.now() - d.t0) / DOLLY_MS);
    const e = 1 - (1 - k) ** 3;
    camera.position.lerpVectors(d.from, d.to, e);
    orbit.current?.update();
    invalidate();
    if (k >= 1) run.current = null;
  });
  return useCallback((to: THREE.Vector3, target: THREE.Vector3) => {
    const still = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    if (still) { camera.position.copy(to); run.current = null; return; }
    const from = to.clone().sub(target).multiplyScalar(1.3).add(target);
    camera.position.copy(from);
    run.current = { from, to, t0: performance.now() };
    invalidate();
  }, [camera, invalidate]);
}

/** Turn the orbit camera a quarter around its target on each new `spin`.
 *  The first render's value is not a press, so it is skipped. */
export function useQuarterTurn(spin: Spin | undefined, orbit: React.RefObject<{ target: THREE.Vector3; update: () => void } | null>,
                               camera: THREE.Camera, invalidate: () => void) {
  const seen = useRef(spin?.n ?? 0);
  useEffect(() => {
    if (!spin || spin.n === seen.current) return;
    seen.current = spin.n;
    const o = orbit.current;
    if (!o) return;
    const v = camera.position.clone().sub(o.target);
    v.applyAxisAngle(new THREE.Vector3(0, 1, 0), spin.dir * Math.PI / 2);
    camera.position.copy(o.target).add(v);
    o.update();
    invalidate();
  }, [spin, orbit, camera, invalidate]);
}

const KEYS: Record<string, 'f' | 'b' | 'l' | 'r' | 'run'> = {
  KeyW: 'f', ArrowUp: 'f', KeyS: 'b', ArrowDown: 'b', KeyA: 'l', ArrowLeft: 'l', KeyD: 'r', ArrowRight: 'r',
  ShiftLeft: 'run', ShiftRight: 'run',
};

/** First-person walk: pointer lock to look, WASD to move at walking pace, a
 *  reticle in the middle of the view that reports the rack it rests on. */
function FpvRig({ W, D, rackMesh, racks, onFocus }: {
  W: number; D: number; rackMesh: React.MutableRefObject<THREE.InstancedMesh | null>;
  racks: FloorRack[]; onFocus: (id: string | null) => void;
}) {
  const held = useRef(new Set<string>());
  const last = useRef<string | null>(null);
  const { camera, raycaster } = useThree();
  const centre = useMemo(() => new THREE.Vector2(0, 0), []);

  useEffect(() => {
    const down = (e: KeyboardEvent) => { if (KEYS[e.code]) { held.current.add(KEYS[e.code]); e.preventDefault(); } };
    const up = (e: KeyboardEvent) => { if (KEYS[e.code]) held.current.delete(KEYS[e.code]); };
    window.addEventListener('keydown', down);
    window.addEventListener('keyup', up);
    return () => { window.removeEventListener('keydown', down); window.removeEventListener('keyup', up); };
  }, []);

  useFrame((_, dt) => {
    const k = held.current;
    const speed = (k.has('run') ? 3.4 : 1.5) * Math.min(dt, 0.1);
    const fwd = new THREE.Vector3();
    camera.getWorldDirection(fwd);
    fwd.y = 0; fwd.normalize();
    const right = new THREE.Vector3().crossVectors(fwd, new THREE.Vector3(0, 1, 0)).normalize();
    const mv = new THREE.Vector3();
    if (k.has('f')) mv.add(fwd);
    if (k.has('b')) mv.sub(fwd);
    if (k.has('r')) mv.add(right);
    if (k.has('l')) mv.sub(right);
    if (mv.lengthSq() > 0) {
      camera.position.add(mv.normalize().multiplyScalar(speed));
      camera.position.x = Math.min(W / 2 - 0.3, Math.max(-W / 2 + 0.3, camera.position.x));
      camera.position.z = Math.min(D / 2 - 0.3, Math.max(-D / 2 + 0.3, camera.position.z));
    }
    camera.position.y = EYE;
    // What the reticle rests on.
    let id: string | null = null;
    if (rackMesh.current) {
      raycaster.setFromCamera(centre, camera);
      const hit = raycaster.intersectObject(rackMesh.current, false)[0];
      if (hit?.instanceId != null && hit.distance < 12) id = racks[hit.instanceId]?.id ?? null;
    }
    if (id !== last.current) { last.current = id; onFocus(id); }
  });

  return <PointerLockControls makeDefault />;
}

// ----------------------------------------------------------- room shell

/** The studio behind the room: a vertical gradient in screen space. */
function Backdrop({ themeTick }: { themeTick: number }) {
  const { scene, invalidate } = useThree();
  useEffect(() => {
    const bg = resolveColor('var(--bg)');
    const light = /^#[0-9a-f]{6}$/i.test(bg) && parseInt(bg.slice(1, 3), 16) + parseInt(bg.slice(3, 5), 16) + parseInt(bg.slice(5, 7), 16) > 384;
    const [top, bottom] = light ? SURF.backdropLight : SURF.backdrop;
    const cv = document.createElement('canvas');
    cv.width = 2; cv.height = 512;
    const c = cv.getContext('2d') as CanvasRenderingContext2D;
    const g = c.createLinearGradient(0, 0, 0, cv.height);
    g.addColorStop(0, top); g.addColorStop(1, bottom);
    c.fillStyle = g; c.fillRect(0, 0, cv.width, cv.height);
    const tex = new THREE.CanvasTexture(cv);
    tex.colorSpace = THREE.SRGBColorSpace;
    scene.background = tex;
    invalidate();
    return () => { if (scene.background === tex) scene.background = null; tex.dispose(); };
  }, [scene, invalidate, themeTick]);
  return null;
}

function Room({ W, D, whiteSpace, walls: walled, seeUnder }: {
  W: number; D: number; whiteSpace: boolean; walls: boolean; seeUnder: boolean;
}) {
  // A raised floor sits PLENUM_H over the slab: the apron and the walls go down to the slab.
  const base = whiteSpace ? -PLENUM_H : 0;
  const tiles = useMemo(() => tileTexture(W, D, !whiteSpace), [W, D, whiteSpace]);
  useEffect(() => () => tiles.dispose(), [tiles]);
  const grid = useMemo(() => (whiteSpace ? tileGrid(W, D) : null), [W, D, whiteSpace]);
  useEffect(() => () => grid?.dispose(), [grid]);
  // Four inward-facing walls: the far ones render, the near ones are culled, so
  // the room reads as a cutaway from any angle.
  const walls: [number, number, number, number][] = [
    [0, -D / 2, 0, W], [0, D / 2, Math.PI, W], [-W / 2, 0, Math.PI / 2, D], [W / 2, 0, -Math.PI / 2, D],
  ];
  return (
    <group>
      {/* Flat, unlit surfaces: the tile white reads as white from any angle,
          the way a drawn floor does, and the side walls take one shade more
          than the back walls so the corner still reads. */}
      <mesh rotation-x={-Math.PI / 2} position={[0, base - 0.003, 0]}>
        <planeGeometry args={[W + 3, D + 3]} />
        <meshBasicMaterial color={SURF.apron} />
      </mesh>
      {/* Keyed: three compiles a non-transparent material with alpha forced to
          1, so flipping `transparent` in place leaves the tiles solid. */}
      <mesh rotation-x={-Math.PI / 2}>
        <planeGeometry args={[W, D]} />
        <meshBasicMaterial key={String(seeUnder)} map={tiles} transparent={seeUnder} opacity={seeUnder ? 0.05 : 1} depthWrite={!seeUnder} />
      </mesh>
      {grid && (
        <lineSegments geometry={grid} position={[0, 0.002, 0]} raycast={() => null}>
          <lineBasicMaterial key={String(seeUnder)} color={SURF.tileLine} transparent={seeUnder} opacity={seeUnder ? 0.35 : 1} />
        </lineSegments>
      )}
      {walled && walls.map(([x, z, ry, len], i) => (
        <group key={i} position={[x, 0, z]} rotation-y={ry}>
          <mesh position={[0, (WALL_H + base) / 2, 0]}>
            <planeGeometry args={[len, WALL_H - base]} />
            <meshBasicMaterial color={i < 2 ? SURF.wall : SURF.wallSide} />
          </mesh>
          {/* the dark cap along the top, culled with its wall */}
          <mesh position={[0, WALL_H - 0.05, 0.001]}>
            <planeGeometry args={[len, 0.1]} />
            <meshBasicMaterial color={SURF.wallEdge} />
          </mesh>
          <mesh position={[0, 0.03, 0.001]}>
            <planeGeometry args={[len, 0.06]} />
            <meshBasicMaterial color={SURF.wallEdge} />
          </mesh>
        </group>
      ))}
    </group>
  );
}

function GridRefs({ W, D, ink }: { W: number; D: number; ink: string }) {
  const cols = Math.max(1, Math.round(W / TILE)), rows = Math.max(1, Math.round(D / TILE));
  const letters = useMemo(() => stripTexture(Array.from({ length: cols }, (_, i) => colLetters(i)), ink), [cols, ink]);
  const numbers = useMemo(() => stripTexture(Array.from({ length: rows }, (_, i) => String(i + 1)), ink), [rows, ink]);
  useEffect(() => () => { letters.dispose(); numbers.dispose(); }, [letters, numbers]);
  const band = TILE * 0.55;
  return (
    <group>
      <mesh rotation-x={-Math.PI / 2} position={[0, 0.004, -D / 2 + band / 2 + 0.03]}>
        <planeGeometry args={[cols * TILE, band]} />
        <meshBasicMaterial map={letters} transparent depthWrite={false} />
      </mesh>
      <mesh rotation={[-Math.PI / 2, 0, -Math.PI / 2]} position={[-W / 2 + band / 2 + 0.03, 0.004, 0]}>
        <planeGeometry args={[rows * TILE, band]} />
        <meshBasicMaterial map={numbers} transparent depthWrite={false} />
      </mesh>
    </group>
  );
}

function colLetters(i: number): string {
  let s = '', n = i;
  do { s = String.fromCharCode(65 + (n % 26)) + s; n = Math.floor(n / 26) - 1; } while (n >= 0);
  return s;
}

function Vents({ vents, X, Z, glass }: {
  vents: { x: number; y: number }[]; X: (x: number) => number; Z: (y: number) => number; glass: boolean;
}) {
  const ref = useRef<THREE.InstancedMesh>(null);
  const tex = useMemo(ventTexture, []);
  useEffect(() => () => tex.dispose(), [tex]);
  useLayoutEffect(() => {
    const im = ref.current;
    if (!im) return;
    const m = new THREE.Matrix4(), q = new THREE.Quaternion().setFromEuler(new THREE.Euler(-Math.PI / 2, 0, 0));
    const p = new THREE.Vector3(), s = new THREE.Vector3(TILE, TILE, 1);
    vents.forEach((v, i) => im.setMatrixAt(i, m.compose(p.set(X(v.x), 0.02, Z(v.y)), q, s)));
    im.instanceMatrix.needsUpdate = true;
    im.computeBoundingSphere();
  }, [vents, X, Z]);
  return (
    <instancedMesh key={vents.length} ref={ref} args={[undefined, undefined, vents.length]}>
      <planeGeometry args={[1, 1]} />
      <meshBasicMaterial key={String(glass)} map={tex} transparent={glass} opacity={glass ? 0.3 : 1} depthWrite={!glass} />
    </instancedMesh>
  );
}

function AisleBand({ a, W, X, Z, racks, containment, tint: tinted = true }: {
  a: FloorAisle; W: number; X: (x: number) => number; Z: (y: number) => number;
  racks: FloorRack[]; containment: boolean;
  /** The floor tint; off while the raised floor is opened. */
  tint?: boolean;
}) {
  const depth = a.y_end - a.y_start;
  const zc = Z((a.y_start + a.y_end) / 2);
  const cold = a.kind !== 'hot';
  const tint = cold ? SURF.coldAisle : SURF.hotAisle;
  const tiles = useMemo(() => (cold ? aisleTexture(Math.round(W / TILE), Math.round(depth / TILE)) : null), [cold, W, depth]);
  useEffect(() => () => tiles?.dispose(), [tiles]);
  const flank = racks.filter((r) => Math.abs(r.y - (a.y_start + a.y_end) / 2) <= depth / 2 + (r.d_m ?? DEFAULT_D));
  const x0 = flank.length ? Math.min(...flank.map((r) => r.x - (r.w_m ?? DEFAULT_W) / 2)) : 0;
  const x1 = flank.length ? Math.max(...flank.map((r) => r.x + (r.w_m ?? DEFAULT_W) / 2)) : 0;
  const h = flank.length ? Math.max(...flank.map(rackH)) : 2;
  if (a.kind === 'unknown') return null;
  return (
    <group>
      {tinted && <mesh rotation-x={-Math.PI / 2} position={[0, 0.004, zc]}>
        <planeGeometry args={[W, depth]} />
        {tiles
          ? <meshBasicMaterial map={tiles} transparent opacity={0.22} depthWrite={false} />
          : <meshBasicMaterial color={tint} transparent opacity={0.09} depthWrite={false} />}
      </mesh>}
      {containment && a.contained && flank.length > 0 && (
        <group>
          <mesh position={[X((x0 + x1) / 2), h + 0.01, zc]}>
            <boxGeometry args={[x1 - x0, 0.02, depth]} />
            <meshBasicMaterial color={tint} transparent opacity={0.16} depthWrite={false} />
          </mesh>
          {[x0, x1].map((xe) => (
            <mesh key={xe} position={[X(xe), h / 2, zc]}>
              <boxGeometry args={[0.02, h, depth]} />
              <meshBasicMaterial color={tint} transparent opacity={0.16} depthWrite={false} />
            </mesh>
          ))}
        </group>
      )}
    </group>
  );
}

// ----------------------------------------------------------- frames + labels

function RackFrames({ racks, X, Z }: { racks: FloorRack[]; X: (x: number) => number; Z: (y: number) => number }) {
  const [edges, fronts] = useMemo(() => {
    const e: number[] = [], f: number[] = [];
    for (const r of racks) {
      const w = (r.w_m ?? DEFAULT_W) / 2, d = (r.d_m ?? DEFAULT_D) / 2, h = rackH(r);
      const cx = X(r.x), cz = Z(r.y);
      const c = [[-w, -d], [w, -d], [w, d], [-w, d]];
      for (let k = 0; k < 4; k++) {
        const [ax, az] = c[k], [bx, bz] = c[(k + 1) % 4];
        e.push(cx + ax, 0, cz + az, cx + bx, 0, cz + bz, cx + ax, h, cz + az, cx + bx, h, cz + bz,
               cx + ax, 0, cz + az, cx + ax, h, cz + az);
      }
      if (r.facing === 'N' || r.facing === 'S') {
        const fz = cz + (r.facing === 'N' ? -d : d) + (r.facing === 'N' ? -0.004 : 0.004);
        for (const y of [0.05, h * 0.5, h - 0.05]) f.push(cx - w, y, fz, cx + w, y, fz);
      }
    }
    const ge = new THREE.BufferGeometry(); ge.setAttribute('position', new THREE.Float32BufferAttribute(e, 3));
    const gf = new THREE.BufferGeometry(); gf.setAttribute('position', new THREE.Float32BufferAttribute(f, 3));
    return [ge, gf];
  }, [racks, X, Z]);
  useEffect(() => () => { edges.dispose(); fronts.dispose(); }, [edges, fronts]);
  return (
    <group>
      <lineSegments geometry={edges}><lineBasicMaterial color={SURF.rackEdge} transparent opacity={0.55} /></lineSegments>
      <lineSegments geometry={fronts}><lineBasicMaterial color={SURF.front} /></lineSegments>
    </group>
  );
}

/** A crisp ring round each partly filled rack at the gauge's level: that edge
 *  is the reading, so it is drawn rather than left to the frost. */
function FillLines({ boxes }: { boxes: GBox[] }) {
  const geom = useMemo(() => {
    const pts: number[] = [];
    for (const b of boxes) {
      const f = b.paint.fill;
      if (!(f > 0 && f < 1)) continue;
      const y = b.y - b.h / 2 + b.h * f, w = b.w / 2 + 0.004, d = b.d / 2 + 0.004;
      const c = [[-w, -d], [w, -d], [w, d], [-w, d]];
      for (let k = 0; k < 4; k++) {
        const [ax, az] = c[k], [bx, bz] = c[(k + 1) % 4];
        pts.push(b.x + ax, y, b.z + az, b.x + bx, y, b.z + bz);
      }
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
    return g;
  }, [boxes]);
  useEffect(() => () => geom.dispose(), [geom]);
  return <lineSegments geometry={geom}><lineBasicMaterial color={SURF.rackEdge} transparent opacity={0.85} /></lineSegments>;
}

function UnitFrames({ boxes }: { boxes: GBox[] }) {
  const geom = useMemo(() => {
    const pts: number[] = [];
    const v = new THREE.Vector3(), q = new THREE.Quaternion(), up = new THREE.Vector3(0, 1, 0);
    for (const b of boxes) {
      q.setFromAxisAngle(up, b.rot);
      const c = (sx: number, sy: number, sz: number) => {
        v.set(sx * b.w / 2, sy * b.h / 2, sz * b.d / 2).applyQuaternion(q);
        return [v.x + b.x, v.y + b.y, v.z + b.z];
      };
      const P = [c(-1, -1, -1), c(1, -1, -1), c(1, -1, 1), c(-1, -1, 1), c(-1, 1, -1), c(1, 1, -1), c(1, 1, 1), c(-1, 1, 1)];
      const E = [[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4], [0, 4], [1, 5], [2, 6], [3, 7]];
      for (const [a, bb] of E) pts.push(...P[a], ...P[bb]);
      // the front: a bar low on the face the unit discharges from (local -z)
      pts.push(...c(-0.9, -0.8, -1.01), ...c(0.9, -0.8, -1.01));
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
    return g;
  }, [boxes]);
  useEffect(() => () => geom.dispose(), [geom]);
  return <lineSegments geometry={geom}><lineBasicMaterial color={SURF.rackEdge} transparent opacity={0.5} /></lineSegments>;
}

function Instruments({ items, X, Z, onSelect, onTip }: {
  items: FloorEquipment[]; X: (x: number) => number; Z: (y: number) => number;
  onSelect?: (s: Sel) => void; onTip?: (t: HoverTip | null) => void;
}) {
  const ref = useRef<THREE.InstancedMesh>(null);
  useLayoutEffect(() => {
    const im = ref.current;
    if (!im) return;
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), p = new THREE.Vector3(), s = new THREE.Vector3(0.12, 0.12, 0.06);
    items.forEach((e, i) => im.setMatrixAt(i, m.compose(p.set(X(e.x as number), e.mount_height_m ?? 1.2, Z(e.y as number)), q, s)));
    im.instanceMatrix.needsUpdate = true;
    im.computeBoundingSphere();
  }, [items, X, Z]);
  if (!items.length) return null;
  return (
    <instancedMesh key={items.length} ref={ref} args={[undefined, undefined, items.length]}
                   onClick={onSelect && ((e) => { e.stopPropagation(); if (e.instanceId != null) onSelect({ kind: 'equipment', id: items[e.instanceId].id }); })}
                   onPointerMove={onTip && ((e) => {
                     e.stopPropagation();
                     if (e.instanceId == null) return;
                     const it = items[e.instanceId];
                     onTip({ x: e.nativeEvent.clientX, y: e.nativeEvent.clientY,
                             text: [it.name, humanise(it.device_type), it.mount].filter(Boolean).join(' · ') });
                   })}
                   onPointerOut={() => onTip?.(null)}>
      <boxGeometry args={[1, 1, 1]} />
      <meshStandardMaterial color={SURF.instrument} />
    </instancedMesh>
  );
}

function Labels({ racks, units, X, Z, ink, bg }: {
  racks: FloorRack[]; units: FloorEquipment[]; X: (x: number) => number; Z: (y: number) => number;
  ink: string; bg: string;
}) {
  const items = useMemo(() => [
    ...racks.map((r) => ({ id: r.id, text: r.name, x: X(r.x), y: rackH(r) + 0.3, z: Z(r.y) })),
    ...units.map((e) => ({ id: e.id, text: e.name.split('-')[0], x: X(e.x as number), y: (e.h_m ?? 1.5) + 0.3, z: Z(e.y as number) })),
  ], [racks, units, X, Z]);
  const textures = useMemo(() => items.map((it) => labelTexture(it.text, ink, bg)), [items, ink, bg]);
  useEffect(() => () => textures.forEach((t) => t.dispose()), [textures]);
  return (
    <group>
      {items.map((it, i) => (
        <sprite key={it.id} position={[it.x, it.y, it.z]} scale={[1.2, 0.3, 1]}>
          <spriteMaterial map={textures[i]} transparent depthTest={false} />
        </sprite>
      ))}
    </group>
  );
}

// ----------------------------------------------------------- devices

function deviceColor(d: TwinDevice): string {
  if (d.status === 'OFFLINE' || d.max_severity !== 'CLEAR') {
    return alarmColor(d.max_severity, d.status === 'OFFLINE' ? 1 : 0);
  }
  if (['switch', 'router', 'firewall', 'load_balancer', 'oob_switch'].includes(d.device_type)) return 'var(--cat-it)';
  if (d.device_type === 'pdu' || d.device_type === 'floor_pdu') return 'var(--cat-pwr)';
  if (d.device_type === 'sensor') return 'var(--cat-cool)';
  return 'var(--border-strong)';
}

/** Where a device's box sits: [x, y, z, width, height, depth] in scene metres. */
export function deviceBox(d: TwinDevice, r: FloorRack, X: (x: number) => number, Z: (y: number) => number,
                          zeroIdx: number): [number, number, number, number, number, number] | null {
  const w = r.w_m ?? DEFAULT_W, dep = r.d_m ?? DEFAULT_D;
  const cx = X(r.x), cz = Z(r.y);
  const front = r.facing === 'N' ? -1 : 1;
  if (d.u_start != null && d.u_start > 0) {
    const h = d.u_height * U - 0.004;
    return [cx, RACK_BASE + (d.u_start - 1) * U + h / 2 + 0.002, cz, w - 0.08, h, dep - 0.12];
  }
  if (d.mount === 'zero_u') {
    const side = zeroIdx % 2 === 0 ? -1 : 1;
    return [cx + side * (w / 2 - 0.05), RACK_BASE + 0.9, cz - front * (dep / 2 - 0.08), 0.05, 1.6, 0.06];
  }
  if (d.mount === 'rack_front' || d.mount === 'rack_rear') {
    const y = d.mount_height_m ?? 1.2;
    const sgn = d.mount === 'rack_front' ? front : -front;
    return [cx + (zeroIdx % 3 - 1) * 0.12, y, cz + sgn * (dep / 2 + 0.02), 0.07, 0.05, 0.03];
  }
  return null;
}

function RackDevices({ devices, rackById, X, Z, themeTick, onSelect, onTip }: {
  devices: TwinDevice[]; rackById: Map<string, FloorRack>;
  X: (x: number) => number; Z: (y: number) => number; themeTick: number;
  onSelect?: (s: Sel) => void; onTip?: (t: HoverTip | null) => void;
}) {
  const ref = useRef<THREE.InstancedMesh>(null);
  const { invalidate } = useThree();
  const boxes = useMemo(() => {
    const perRack = new Map<string, number>();
    return devices.map((d) => {
      const r = rackById.get(d.rack_id);
      if (!r) return null;
      const n = perRack.get(d.rack_id) ?? 0;
      if (d.u_start == null) perRack.set(d.rack_id, n + 1);
      return deviceBox(d, r, X, Z, n);
    });
  }, [devices, rackById, X, Z]);
  const colors = useMemo(() => devices.map((d) => new THREE.Color(resolveColor(deviceColor(d)))),
    [devices, themeTick]); // eslint-disable-line react-hooks/exhaustive-deps

  useLayoutEffect(() => {
    const im = ref.current;
    if (!im) return;
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), p = new THREE.Vector3(), s = new THREE.Vector3();
    boxes.forEach((b, i) => {
      if (!b) m.makeScale(0, 0, 0);
      else m.compose(p.set(b[0], b[1], b[2]), q, s.set(b[3], b[4], b[5]));
      im.setMatrixAt(i, m);
      im.setColorAt(i, colors[i]);
    });
    im.instanceMatrix.needsUpdate = true;
    if (im.instanceColor) im.instanceColor.needsUpdate = true;
    im.computeBoundingSphere();
    invalidate();
  }, [boxes, colors, invalidate]);

  if (!devices.length) return null;
  return (
    <instancedMesh key={devices.length} ref={ref} args={[undefined, undefined, devices.length]}
                   onClick={onSelect && ((e) => { e.stopPropagation(); if (e.instanceId != null) onSelect({ kind: 'device', id: devices[e.instanceId].id }); })}
                   onPointerMove={onTip && ((e) => {
                     e.stopPropagation();
                     if (e.instanceId == null) return;
                     const d = devices[e.instanceId];
                     onTip({ x: e.nativeEvent.clientX, y: e.nativeEvent.clientY,
                             text: [d.name, humanise(d.device_type), d.u_start ? `U${d.u_start}` : d.mount,
                                    d.temp_c != null ? fmtT(d.temp_c) : null,
                                    d.power_w != null ? `${(d.power_w / 1000).toFixed(2)} kW` : null,
                                    d.status.toLowerCase(),
                                    d.max_severity !== 'CLEAR' ? d.max_severity.toLowerCase() : null]
                               .filter(Boolean).join(' · ') });
                   })}
                   onPointerOut={() => onTip?.(null)}>
      <boxGeometry args={[1, 1, 1]} />
      <meshStandardMaterial roughness={0.7} />
    </instancedMesh>
  );
}

function Highlight({ sel, rackBoxes, racks, unitBoxes, units, devices, rackById, X, Z, color }: {
  sel: Sel; rackBoxes: GBox[]; racks: FloorRack[]; unitBoxes: GBox[]; units: FloorEquipment[];
  devices: TwinDevice[]; rackById: Map<string, FloorRack>;
  X: (x: number) => number; Z: (y: number) => number; color: string;
}) {
  let box: GBox | null = null;
  let name = '';
  if (sel?.kind === 'rack') {
    const i = racks.findIndex((r) => r.id === sel.id);
    if (i >= 0) { box = rackBoxes[i]; name = racks[i].name; }
  } else if (sel?.kind === 'equipment') {
    const i = units.findIndex((e) => e.id === sel.id);
    if (i >= 0) { box = unitBoxes[i]; name = units[i].name; }
  } else if (sel?.kind === 'device') {
    const d = devices.find((x) => x.id === sel.id);
    const r = d && rackById.get(d.rack_id);
    const b = d && r ? deviceBox(d, r, X, Z, 0) : null;
    if (b) box = { x: b[0], y: b[1], z: b[2], w: b[3], h: b[4], d: b[5], rot: 0, front: 0,
                   paint: { bot: '', mid: '', top: '', fill: 1 } };
    if (d) name = d.name;
  }
  const [bw, bh, bd] = box ? [box.w + 0.04, box.h + 0.04, box.d + 0.04] : [0, 0, 0];
  const geom = useMemo(() => new THREE.EdgesGeometry(new THREE.BoxGeometry(bw, bh, bd)), [bw, bh, bd]);
  const glow = useMemo(() => new THREE.BoxGeometry(bw + 0.06, bh + 0.06, bd + 0.06), [bw, bh, bd]);
  useEffect(() => () => { geom.dispose(); glow.dispose(); }, [geom, glow]);
  if (!box) return null;
  // The selection: a soft glow of the accent around the box, the edges on
  // top of it, and the name floating above - so what is picked reads as
  // picked from any angle and at any zoom, without a loud yellow frame.
  return (
    <group position={[box.x, box.y, box.z]} rotation-y={box.rot}>
      <mesh geometry={glow} renderOrder={5}>
        <meshBasicMaterial color={color} transparent opacity={0.16} depthWrite={false} />
      </mesh>
      <lineSegments geometry={geom}>
        <lineBasicMaterial color={color} linewidth={2} />
      </lineSegments>
      {name && (
        <Html position={[0, bh / 2 + 0.25, 0]} center zIndexRange={[3, 3]} style={{ pointerEvents: 'none' }}>
          <span className="vw-seltag">{name}</span>
        </Html>
      )}
    </group>
  );
}
