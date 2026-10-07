import {
  MapControls, OrbitControls, OrthographicCamera, PerspectiveCamera, PointerLockControls,
} from '@react-three/drei';
import { useFrame, useThree, type ThreeEvent } from '@react-three/fiber';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, type ElementRef } from 'react';
import * as THREE from 'three';
import type { FloorAisle, FloorEquipment, FloorRack, ThermalUnit, TwinDevice, TwinRoomScene } from '../../api/client';
import { humanise } from '../../lib/format';
import { alarmColor, resolveColor } from './colors';
import {
  paintEquipment, paintRack, rackTiers, unitReadings, ventTiles, RACK_BASE, U, TILE,
  type CoolingLayer, type RackLayer, type RackPaint, type Sel, type ViewMode, type Visibility,
} from './layers';

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
const EYE = 1.6;

// The physical surfaces - concrete, steel, tile - are the same under both
// themes, like the readings' bands; only the chrome around the picture follows
// the theme. These are a rendering of the room, not UI colour.
const SURF = {
  floor: '#eef0f3', tileLine: '#cfd4da', slab: '#dadde1', slabLine: '#c6cad0',
  ventBg: '#e2e6ea', ventDot: '#848c96', wall: '#d9dde3', wallEdge: '#9aa1ab',
  rackEdge: '#23272e', front: '#5b6470', instrument: '#4d8fa6',
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
  resetTick: number;
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

const GRADIENT_VERT = /* glsl */`
  attribute vec3 cBot; attribute vec3 cMid; attribute vec3 cTop;
  varying vec3 vBot; varying vec3 vMid; varying vec3 vTop; varying float vT; varying vec3 vN;
  void main() {
    vBot = cBot; vMid = cMid; vTop = cTop;
    vT = position.y + 0.5;
    vN = normalize((modelMatrix * instanceMatrix * vec4(normal, 0.0)).xyz);
    gl_Position = projectionMatrix * modelViewMatrix * instanceMatrix * vec4(position, 1.0);
  }`;
const GRADIENT_FRAG = /* glsl */`
  uniform vec3 uLight; uniform float uOpacity;
  varying vec3 vBot; varying vec3 vMid; varying vec3 vTop; varying float vT; varying vec3 vN;
  void main() {
    vec3 c = vT < 0.5 ? mix(vBot, vMid, vT * 2.0) : mix(vMid, vTop, (vT - 0.5) * 2.0);
    float l = 0.64 + 0.36 * max(dot(normalize(vN), normalize(uLight)), 0.0);
    gl_FragColor = vec4(c * l, uOpacity);
    #include <colorspace_fragment>
  }`;

export interface GBox {
  x: number; y: number; z: number;      // centre of the FULL box
  w: number; h: number; d: number;
  rot: number;                          // radians about +y
  paint: RackPaint;
}

/** One instanced mesh of boxes, each with its own bottom/middle/top colour.
 *  `part` 'solid' draws the filled share of each box, 'shell' the translucent
 *  remainder above it (only for boxes that are not full). */
function GradientBoxes({ boxes, part, opacity, meshRef, onClick, onMove, onOut }: {
  boxes: GBox[]; part: 'solid' | 'shell'; opacity: number;
  meshRef?: React.MutableRefObject<THREE.InstancedMesh | null>;
  onClick?: (i: number) => void; onMove?: (i: number, e: ThreeEvent<PointerEvent>) => void; onOut?: () => void;
}) {
  const inner = useRef<THREE.InstancedMesh>(null);
  const { invalidate } = useThree();
  const n = boxes.length;

  const geometry = useMemo(() => {
    const g = new THREE.BoxGeometry(1, 1, 1);
    for (const name of ['cBot', 'cMid', 'cTop']) {
      g.setAttribute(name, new THREE.InstancedBufferAttribute(new Float32Array(Math.max(1, n) * 3), 3));
    }
    return g;
  }, [n]);
  const material = useMemo(() => new THREE.ShaderMaterial({
    vertexShader: GRADIENT_VERT, fragmentShader: GRADIENT_FRAG,
    uniforms: { uLight: { value: new THREE.Vector3(0.45, 1, 0.55) }, uOpacity: { value: opacity } },
    transparent: opacity < 1, depthWrite: opacity >= 1,
  }), []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    material.uniforms.uOpacity.value = opacity;
    material.transparent = opacity < 1;
    material.depthWrite = opacity >= 1;
    material.needsUpdate = true;
  }, [material, opacity]);
  useEffect(() => () => { geometry.dispose(); }, [geometry]);
  useEffect(() => () => { material.dispose(); }, [material]);

  useLayoutEffect(() => {
    const im = inner.current;
    if (!im) return;
    if (meshRef) meshRef.current = im;
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), p = new THREE.Vector3(), s = new THREE.Vector3();
    const up = new THREE.Vector3(0, 1, 0);
    const col = new THREE.Color();
    const attr = (k: string) => geometry.getAttribute(k) as THREE.InstancedBufferAttribute;
    const aB = attr('cBot'), aM = attr('cMid'), aT = attr('cTop');
    boxes.forEach((b, i) => {
      const fill = Math.min(1, Math.max(0, b.paint.fill));
      let h: number, yc: number;
      if (part === 'solid') { h = b.h * fill; yc = b.y - b.h / 2 + h / 2; }
      else { h = fill < 1 ? b.h * (1 - fill) : 0; yc = b.y + b.h / 2 - h / 2; }
      q.setFromAxisAngle(up, b.rot);
      im.setMatrixAt(i, m.compose(p.set(b.x, yc, b.z), q, s.set(b.w, Math.max(h, 0), b.d)));
      for (const [a, c] of [[aB, b.paint.bot], [aM, b.paint.mid], [aT, b.paint.top]] as const) {
        col.set(part === 'shell' ? SURF.wall : c);
        a.setXYZ(i, col.r, col.g, col.b);
      }
    });
    im.instanceMatrix.needsUpdate = true;
    aB.needsUpdate = aM.needsUpdate = aT.needsUpdate = true;
    im.computeBoundingSphere();
    invalidate();
  }, [boxes, part, geometry, invalidate, meshRef]);

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
  t.anisotropy = 4;
  if (repeat) { t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(repeat[0], repeat[1]); }
  return t;
}

function tileTexture(W: number, D: number, slab: boolean): THREE.CanvasTexture {
  return canvasTexture(128, (c, s) => {
    c.fillStyle = slab ? SURF.slab : SURF.floor; c.fillRect(0, 0, s, s);
    c.strokeStyle = slab ? SURF.slabLine : SURF.tileLine; c.lineWidth = slab ? 1 : 2;
    c.strokeRect(0.5, 0.5, s - 1, s - 1);
  }, [W / (slab ? 1.0 : TILE), D / (slab ? 1.0 : TILE)]);
}

function ventTexture(): THREE.CanvasTexture {
  return canvasTexture(128, (c, s) => {
    c.fillStyle = SURF.ventBg; c.fillRect(0, 0, s, s);
    c.strokeStyle = SURF.tileLine; c.lineWidth = 2; c.strokeRect(1, 1, s - 2, s - 2);
    c.fillStyle = SURF.ventDot;
    for (let y = 12; y < s - 6; y += 10) for (let x = 12; x < s - 6; x += 10) {
      c.beginPath(); c.arc(x, y, 2.4, 0, Math.PI * 2); c.fill();
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

function labelTexture(text: string, ink: string, bg: string): THREE.CanvasTexture {
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
  const { data, units, rackLayer, coolingLayer, vis, mode, sel, resetTick, themeTick, onSelect, onTip, onFocus } = p;
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
               paint: paintRack(r, byRack.get(r.id) ?? [], rackLayer, peak) };
    });
  }, [plan.racks, byRack, rackLayer, X, Z, themeTick]); // eslint-disable-line react-hooks/exhaustive-deps

  const placedUnits = useMemo(() => plan.equipment.filter((e) => e.w_m != null && e.d_m != null), [plan.equipment]);
  const instruments = useMemo(() => plan.equipment.filter((e) => e.w_m == null || e.d_m == null), [plan.equipment]);
  const unitBoxes = useMemo<GBox[]>(() => placedUnits.map((e) => {
    const h = e.h_m ?? 1.5;
    const y = e.mount === 'wall' ? (e.mount_height_m ?? h / 2) : h / 2;
    return { x: X(e.x as number), y, z: Z(e.y as number), w: e.w_m as number, h, d: e.d_m as number,
             rot: -((e.facing_deg ?? 0) * Math.PI) / 180,
             paint: paintEquipment(e, coolingLayer, unitReadings(e, units.get(e.id))) };
  }), [placedUnits, units, coolingLayer, X, Z, themeTick]); // eslint-disable-line react-hooks/exhaustive-deps

  const vents = useMemo(() => ventTiles(plan.racks, plan.aisles, whiteSpace), [plan.racks, plan.aisles, whiteSpace]);

  // --- tooltips -----------------------------------------------------------
  const tip = (e: ThreeEvent<PointerEvent>, text: string) =>
    onTip({ x: e.nativeEvent.clientX, y: e.nativeEvent.clientY, text });
  const rackText = (r: FloorRack) => {
    const [b, m, t] = rackTiers(byRack.get(r.id) ?? [], r.u_height ?? 42);
    const f = (v: number | null) => (v == null ? '–' : `${v.toFixed(1)}`);
    return [r.name, `${r.device_count} devices`,
      b != null || t != null ? `inlet ${f(b)} / ${f(m)} / ${f(t)} °C` : 'no inlet reading',
      r.load_kw != null ? `${r.load_kw.toFixed(1)} kW` : null,
      r.free_u != null ? `${r.free_u} U free` : null].filter(Boolean).join(' · ');
  };
  const unitText = (e: FloorEquipment) => {
    const u = unitReadings(e, units.get(e.id));
    return [e.name, humanise(e.device_type),
      u.supply_c != null ? `supply ${u.supply_c.toFixed(1)} °C` : null,
      u.return_c != null ? `return ${u.return_c.toFixed(1)} °C` : null,
      e.power_w != null ? `${(e.power_w / 1000).toFixed(1)} kW` : null,
      e.max_severity !== 'CLEAR' ? e.max_severity.toLowerCase() : null].filter(Boolean).join(' · ');
  };

  const rackMesh = useRef<THREE.InstancedMesh | null>(null);
  const fpv = mode === 'fpv';
  const anyShell = rackBoxes.some((b) => b.paint.fill < 1);

  return (
    <>
      <color attach="background" args={[tones.bg]} />
      <hemisphereLight args={['#ffffff', '#b9bec6', 0.9]} />
      <directionalLight position={[W * 0.6, 14, D * 0.4]} intensity={0.55} />

      <Cameras mode={mode} W={W} D={D} resetTick={resetTick} aisles={plan.aisles} X={X} Z={Z} />
      {fpv && <FpvRig W={W} D={D} rackMesh={rackMesh} racks={plan.racks} onFocus={onFocus} />}

      <Room W={W} D={D} whiteSpace={whiteSpace} />
      <GridRefs W={W} D={D} ink={tones.inkMuted} />
      {vis.vents && vents.length > 0 && <Vents vents={vents} X={X} Z={Z} />}
      {vis.aisles && plan.aisles.map((a) => (
        <AisleBand key={`${a.label}-${a.y_start}`} a={a} W={W} Z={Z} X={X} racks={plan.racks}
                   tones={tones} containment={vis.containment} />
      ))}

      <GradientBoxes boxes={rackBoxes} part="solid" opacity={1} meshRef={rackMesh}
                     onClick={fpv ? undefined : (i) => onSelect({ kind: 'rack', id: plan.racks[i].id })}
                     onMove={fpv ? undefined : (i, e) => tip(e, rackText(plan.racks[i]))}
                     onOut={() => onTip(null)} />
      {anyShell && <GradientBoxes boxes={rackBoxes} part="shell" opacity={0.22} />}
      <RackFrames racks={plan.racks} X={X} Z={Z} />
      {vis.devices && (
        <RackDevices devices={devices} rackById={rackById} X={X} Z={Z} themeTick={themeTick}
                     onSelect={fpv ? undefined : onSelect} onTip={fpv ? undefined : onTip} />
      )}

      {vis.plant && (
        <>
          <GradientBoxes boxes={unitBoxes} part="solid" opacity={1}
                         onClick={fpv ? undefined : (i) => onSelect({ kind: 'equipment', id: placedUnits[i].id })}
                         onMove={fpv ? undefined : (i, e) => tip(e, unitText(placedUnits[i]))}
                         onOut={() => onTip(null)} />
          <UnitFrames boxes={unitBoxes} />
          <Instruments items={instruments} X={X} Z={Z}
                       onSelect={fpv ? undefined : onSelect} onTip={fpv ? undefined : onTip} />
        </>
      )}

      {vis.labels && <Labels racks={plan.racks} units={placedUnits} X={X} Z={Z}
                             ink={tones.ink} bg={tones.raised} />}
      <Highlight sel={sel} rackBoxes={rackBoxes} racks={plan.racks} unitBoxes={unitBoxes} units={placedUnits}
                 devices={devices} rackById={rackById} X={X} Z={Z} color={tones.accent} />
      <Invalidator deps={[rackLayer, coolingLayer, vis, sel, mode]} invalidate={invalidate} />
    </>
  );
}

function Invalidator({ deps, invalidate }: { deps: unknown[]; invalidate: () => void }) {
  useEffect(() => { invalidate(); }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

// ----------------------------------------------------------- cameras

function Cameras({ mode, W, D, resetTick, aisles, X, Z }: {
  mode: ViewMode; W: number; D: number; resetTick: number; aisles: FloorAisle[];
  X: (x: number) => number; Z: (y: number) => number;
}) {
  const orbit = useRef<ElementRef<typeof OrbitControls>>(null);
  const map = useRef<ElementRef<typeof MapControls>>(null);
  const { camera, size, invalidate } = useThree();
  const span = Math.max(W, D);

  useEffect(() => {
    if (mode === '3d') {
      camera.position.set(span * 0.7, span * 0.75, span * 0.95);
      orbit.current?.target.set(0, 0.8, 0);
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
  }, [mode, resetTick, camera, span, W, D, size.width, size.height, aisles, X, Z, invalidate]);

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

function Room({ W, D, whiteSpace }: { W: number; D: number; whiteSpace: boolean }) {
  const tiles = useMemo(() => tileTexture(W, D, !whiteSpace), [W, D, whiteSpace]);
  useEffect(() => () => tiles.dispose(), [tiles]);
  const edges = useMemo(() => new THREE.EdgesGeometry(new THREE.BoxGeometry(W, WALL_H, D)), [W, D]);
  // Four inward-facing walls: the far ones render, the near ones are culled, so
  // the room reads as a cutaway from any angle.
  const walls: [number, number, number, number][] = [
    [0, -D / 2, 0, W], [0, D / 2, Math.PI, W], [-W / 2, 0, Math.PI / 2, D], [W / 2, 0, -Math.PI / 2, D],
  ];
  return (
    <group>
      <mesh rotation-x={-Math.PI / 2} receiveShadow>
        <planeGeometry args={[W, D]} />
        <meshStandardMaterial map={tiles} roughness={0.95} />
      </mesh>
      {walls.map(([x, z, ry, len], i) => (
        <mesh key={i} position={[x, WALL_H / 2, z]} rotation-y={ry}>
          <planeGeometry args={[len, WALL_H]} />
          <meshStandardMaterial color={SURF.wall} roughness={1} />
        </mesh>
      ))}
      <lineSegments geometry={edges} position={[0, WALL_H / 2, 0]}>
        <lineBasicMaterial color={SURF.wallEdge} />
      </lineSegments>
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

function Vents({ vents, X, Z }: { vents: { x: number; y: number }[]; X: (x: number) => number; Z: (y: number) => number }) {
  const ref = useRef<THREE.InstancedMesh>(null);
  const tex = useMemo(ventTexture, []);
  useEffect(() => () => tex.dispose(), [tex]);
  useLayoutEffect(() => {
    const im = ref.current;
    if (!im) return;
    const m = new THREE.Matrix4(), q = new THREE.Quaternion().setFromEuler(new THREE.Euler(-Math.PI / 2, 0, 0));
    const p = new THREE.Vector3(), s = new THREE.Vector3(TILE * 0.96, TILE * 0.96, 1);
    vents.forEach((v, i) => im.setMatrixAt(i, m.compose(p.set(X(v.x), 0.006, Z(v.y)), q, s)));
    im.instanceMatrix.needsUpdate = true;
    im.computeBoundingSphere();
  }, [vents, X, Z]);
  return (
    <instancedMesh key={vents.length} ref={ref} args={[undefined, undefined, vents.length]}>
      <planeGeometry args={[1, 1]} />
      <meshBasicMaterial map={tex} />
    </instancedMesh>
  );
}

function AisleBand({ a, W, X, Z, racks, tones, containment }: {
  a: FloorAisle; W: number; X: (x: number) => number; Z: (y: number) => number;
  racks: FloorRack[]; tones: { cold: string; hot: string }; containment: boolean;
}) {
  const depth = a.y_end - a.y_start;
  const zc = Z((a.y_start + a.y_end) / 2);
  const tint = a.kind === 'hot' ? tones.hot : tones.cold;
  const flank = racks.filter((r) => Math.abs(r.y - (a.y_start + a.y_end) / 2) <= depth / 2 + (r.d_m ?? DEFAULT_D));
  const x0 = flank.length ? Math.min(...flank.map((r) => r.x - (r.w_m ?? DEFAULT_W) / 2)) : 0;
  const x1 = flank.length ? Math.max(...flank.map((r) => r.x + (r.w_m ?? DEFAULT_W) / 2)) : 0;
  const h = flank.length ? Math.max(...flank.map(rackH)) : 2;
  if (a.kind === 'unknown') return null;
  return (
    <group>
      <mesh rotation-x={-Math.PI / 2} position={[0, 0.003, zc]}>
        <planeGeometry args={[W, depth]} />
        <meshBasicMaterial color={tint} transparent opacity={0.1} depthWrite={false} />
      </mesh>
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
                                    d.temp_c != null ? `${d.temp_c.toFixed(1)} °C` : null,
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
  if (sel?.kind === 'rack') {
    const i = racks.findIndex((r) => r.id === sel.id);
    if (i >= 0) box = rackBoxes[i];
  } else if (sel?.kind === 'equipment') {
    const i = units.findIndex((e) => e.id === sel.id);
    if (i >= 0) box = unitBoxes[i];
  } else if (sel?.kind === 'device') {
    const d = devices.find((x) => x.id === sel.id);
    const r = d && rackById.get(d.rack_id);
    const b = d && r ? deviceBox(d, r, X, Z, 0) : null;
    if (b) box = { x: b[0], y: b[1], z: b[2], w: b[3], h: b[4], d: b[5], rot: 0, paint: { bot: '', mid: '', top: '', fill: 1 } };
  }
  const [bw, bh, bd] = box ? [box.w + 0.04, box.h + 0.04, box.d + 0.04] : [0, 0, 0];
  const geom = useMemo(() => new THREE.EdgesGeometry(new THREE.BoxGeometry(bw, bh, bd)), [bw, bh, bd]);
  useEffect(() => () => geom.dispose(), [geom]);
  if (!box) return null;
  return (
    <lineSegments geometry={geom} position={[box.x, box.y, box.z]} rotation-y={box.rot}>
      <lineBasicMaterial color={color} linewidth={2} />
    </lineSegments>
  );
}
