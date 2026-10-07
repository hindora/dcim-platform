import { useQuery } from '@tanstack/react-query';
import { OrbitControls } from '@react-three/drei';
import { Canvas, useThree, type ThreeEvent } from '@react-three/fiber';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ElementRef } from 'react';
import { Link } from 'react-router-dom';
import * as THREE from 'three';
import {
  api, type FloorAisle, type FloorEquipment, type FloorRack, type TwinDevice, type TwinRoomScene,
} from '../../api/client';
import { Seg } from '../../components/estate';
import { humanise } from '../../lib/format';
import { alarmColor, rackFill, resolveColor, type Overlay } from './colors';
import './floorplan3d.css';

/**
 * One room in 3D (docs/27 Phase 1): racks with their contents, floor-standing
 * plant at its true footprint, aisles and containment - coloured by the same
 * overlays as the 2D plan, from the same scales (colors.ts).
 *
 * Built to stay cheap on integrated graphics: racks and rack devices are each
 * ONE instanced mesh (a few draw calls for a few hundred boxes), rack edges are
 * one merged line set, and the canvas renders on demand rather than every
 * frame. No text is drawn in WebGL - SDF text would fetch a font from a CDN,
 * which an air-gapped install cannot - so names live in the DOM tooltip and the
 * side card.
 *
 * Coordinates: room metres from the import, x along the rows, y across them;
 * in the scene x stays x, the room's y becomes z, and up is +y.
 */

const U = 0.04445;          // one rack unit (EIA-310)
const RACK_BASE = 0.1;      // U1's bottom edge above the floor
const WALL_H = 3.0;         // clear height drawn for the room's walls
const DEFAULT_W = 0.6, DEFAULT_D = 1.2;

type Preset = 'iso' | 'top' | 'front';
type Sel = { kind: 'rack' | 'device' | 'equipment'; id: string } | null;
type Tip = { x: number; y: number; text: string } | null;

const NETWORK = new Set(['switch', 'router', 'firewall', 'load_balancer', 'oob_switch']);
const COOLING = new Set(['crah', 'crac', 'chiller', 'pump', 'cooling_tower', 'cdu', 'valve']);

function hasWebGL(): boolean {
  try {
    const c = document.createElement('canvas');
    return Boolean(c.getContext('webgl2') || c.getContext('webgl'));
  } catch {
    return false;
  }
}

function rackH(r: FloorRack): number {
  return RACK_BASE + (r.u_height ?? 42) * U;
}

/** Device fill: a state colour only when something is wrong with it, so a rack
 *  of healthy servers reads as a quiet block and a fault stands out of it. */
function deviceColor(d: TwinDevice): string {
  if (d.status === 'OFFLINE' || d.max_severity !== 'CLEAR') {
    return alarmColor(d.max_severity, d.status === 'OFFLINE' ? 1 : 0);
  }
  if (NETWORK.has(d.device_type)) return 'var(--cat-it)';
  if (d.device_type === 'pdu' || d.device_type === 'floor_pdu') return 'var(--cat-pwr)';
  if (d.device_type === 'sensor') return 'var(--cat-cool)';
  return 'var(--border-strong)';
}

function equipColor(e: FloorEquipment, overlay: Overlay): string {
  if (overlay === 'alarm' || e.max_severity !== 'CLEAR') return alarmColor(e.max_severity, 0);
  if (COOLING.has(e.device_type)) return 'var(--cat-cool)';
  return 'var(--cat-pwr)';
}

export default function FloorPlan3D({ roomId, overlay }: { roomId: string; overlay: Overlay }) {
  const [preset, setPreset] = useState<{ p: Preset; n: number }>({ p: 'iso', n: 0 });
  const [sel, setSel] = useState<Sel>(null);
  const [tip, setTip] = useState<Tip>(null);
  const [theme, setTheme] = useState(0);
  const webgl = useMemo(hasWebGL, []);

  const scene = useQuery<TwinRoomScene>({
    queryKey: ['twin-scene', roomId],
    queryFn: () => api.roomScene(roomId),
    refetchInterval: 15_000,
    retry: false,
  });

  // Theme switches change the tokens under every colour; re-resolve them.
  useEffect(() => {
    const mo = new MutationObserver(() => setTheme((t) => t + 1));
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class'] });
    return () => mo.disconnect();
  }, []);

  useEffect(() => { setSel(null); }, [roomId]);

  if (!webgl) {
    return <p className="muted">This browser has no WebGL, so the 3D view cannot draw. The 2D plan shows the same room.</p>;
  }
  if (scene.isLoading) return <p className="muted">Loading the room…</p>;
  if (scene.isError || !scene.data) {
    return <p className="muted">Nothing in this room is positioned, so it cannot be drawn.</p>;
  }
  const data = scene.data;
  const { plan } = data;

  return (
    <div className="room3d">
      <div className="room3d-stage">
        <div className="room3d-tools">
          <Seg label="Camera" value={preset.p}
               onChange={(p) => setPreset((s) => ({ p, n: s.n + 1 }))}
               options={[{ key: 'iso', label: 'Iso' }, { key: 'top', label: 'Top' },
                         { key: 'front', label: 'Front' }]} />
        </div>
        <Canvas frameloop="demand" dpr={[1, 2]} shadows={false}
                camera={{ fov: 45, near: 0.1, far: 500, position: [10, 10, 10] }}
                onPointerMissed={() => setSel(null)}
                aria-label={`3D view of ${plan.room_name}`}>
          <Room data={data} overlay={overlay} sel={sel} onSelect={setSel} onTip={setTip}
                preset={preset} themeTick={theme} />
        </Canvas>
        {tip && <span className="hover-tip room3d-tip" role="tooltip"
                      style={{ left: tip.x + 14, top: tip.y + 14 }}>{tip.text}</span>}
      </div>
      <aside className="asset-panel room3d-side">
        <SelectionCard data={data} sel={sel} />
        <p className="muted room3d-help">
          Drag to orbit, right-drag to pan, scroll to zoom. Click a rack, a device
          or a unit for its detail.
        </p>
        <p className="muted room3d-help">
          {plan.racks.length} racks · {data.devices.length} devices in them ·{' '}
          {plan.equipment.length} other items placed
          {plan.unpositioned_equipment.length > 0
            && ` · ${plan.unpositioned_equipment.length} without a position (not drawn)`}
        </p>
      </aside>
    </div>
  );
}

function Room({ data, overlay, sel, onSelect, onTip, preset, themeTick }: {
  data: TwinRoomScene; overlay: Overlay; sel: Sel;
  onSelect: (s: Sel) => void; onTip: (t: Tip) => void;
  preset: { p: Preset; n: number }; themeTick: number;
}) {
  const { plan, devices } = data;
  const W = plan.extent.width_m, D = plan.extent.depth_m;
  const X = useCallback((x: number) => x - W / 2, [W]);
  const Z = useCallback((y: number) => y - D / 2, [D]);
  const controls = useRef<ElementRef<typeof OrbitControls>>(null);
  const { camera, invalidate } = useThree();
  // themeTick is a dependency so a theme change re-resolves every token.
  const tones = useMemo(() => ({
    bg: resolveColor('var(--bg-inset)'), floor: resolveColor('var(--bg-raised)'),
    line: resolveColor('var(--border-strong)'), wall: resolveColor('var(--border)'),
    cold: resolveColor('var(--cat-cap)'), hot: resolveColor('var(--critical)'),
    accent: resolveColor('var(--accent)'), front: resolveColor('var(--text-muted)'),
  }), [themeTick]);

  useEffect(() => {
    const span = Math.max(W, D);
    if (preset.p === 'top') camera.position.set(0, span * 1.25, 0.01);
    else if (preset.p === 'front') camera.position.set(0, 1.7, D / 2 + span * 0.8);
    else camera.position.set(span * 0.75, span * 0.8, span * 0.9);
    controls.current?.target.set(0, 0.8, 0);
    controls.current?.update();
    invalidate();
  }, [preset.n, preset.p, W, D, camera, invalidate]);

  const rackById = useMemo(() => new Map(plan.racks.map((r) => [r.id, r])), [plan.racks]);
  const rackColors = useMemo(() => {
    const peakKw = Math.max(0, ...plan.racks.map((r) => r.load_kw ?? 0));
    return plan.racks.map((r) => new THREE.Color(resolveColor(rackFill(r, overlay, peakKw))));
  }, [plan.racks, overlay, themeTick]);
  const deviceColors = useMemo(
    () => devices.map((d) => new THREE.Color(resolveColor(deviceColor(d)))),
    [devices, themeTick]);

  return (
    <>
      <color attach="background" args={[tones.bg]} />
      <ambientLight intensity={0.75} />
      <directionalLight position={[W, 12, D * 0.6]} intensity={0.9} />
      <OrbitControls ref={controls} makeDefault enableDamping={false}
                     maxPolarAngle={Math.PI / 2.05} minDistance={1.5}
                     maxDistance={Math.max(W, D) * 4} />

      <Floor W={W} D={D} whiteSpace={plan.room_class === 'white_space'} tones={tones} />
      {plan.aisles.map((a) => (
        <AisleBand key={`${a.label}-${a.y_start}`} a={a} W={W} Z={Z} X={X}
                   racks={plan.racks} tones={tones} />
      ))}

      <Racks racks={plan.racks} X={X} Z={Z} sel={sel}
             colors={rackColors}
             onSelect={onSelect} onTip={onTip} tones={tones} />
      <RackDevices devices={devices} rackById={rackById} X={X} Z={Z}
                   colors={deviceColors}
                   onSelect={onSelect} onTip={onTip} />
      {plan.equipment.map((e) => (
        <Equipment key={e.id} e={e} X={X} Z={Z}
                   color={new THREE.Color(resolveColor(equipColor(e, overlay)))}
                   selected={sel?.kind === 'equipment' && sel.id === e.id}
                   tones={tones} onSelect={onSelect} onTip={onTip} />
      ))}
      <Highlight sel={sel} rackById={rackById} devices={devices} X={X} Z={Z} color={tones.accent} />
    </>
  );
}

type Tones = Record<'bg' | 'floor' | 'line' | 'wall' | 'cold' | 'hot' | 'accent' | 'front', string>;

function Floor({ W, D, whiteSpace, tones }: { W: number; D: number; whiteSpace: boolean; tones: Tones }) {
  // A raised floor is a 600 mm tile grid; a plant room is a slab.
  const grid = useMemo(() => {
    const pts: number[] = [];
    if (whiteSpace) {
      for (let x = 0; x <= W + 1e-6; x += 0.6) pts.push(x - W / 2, 0.002, -D / 2, x - W / 2, 0.002, D / 2);
      for (let z = 0; z <= D + 1e-6; z += 0.6) pts.push(-W / 2, 0.002, z - D / 2, W / 2, 0.002, z - D / 2);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
    return g;
  }, [W, D, whiteSpace]);
  const walls = useMemo(() => new THREE.EdgesGeometry(new THREE.BoxGeometry(W, WALL_H, D)), [W, D]);
  return (
    <group>
      <mesh rotation-x={-Math.PI / 2} position={[0, 0, 0]}>
        <planeGeometry args={[W, D]} />
        <meshStandardMaterial color={tones.floor} />
      </mesh>
      <lineSegments geometry={grid}>
        <lineBasicMaterial color={tones.wall} transparent opacity={0.6} />
      </lineSegments>
      <lineSegments geometry={walls} position={[0, WALL_H / 2, 0]}>
        <lineBasicMaterial color={tones.line} />
      </lineSegments>
    </group>
  );
}

function AisleBand({ a, W, X, Z, racks, tones }: {
  a: FloorAisle; W: number; X: (x: number) => number; Z: (y: number) => number;
  racks: FloorRack[]; tones: Tones;
}) {
  const depth = a.y_end - a.y_start;
  const zc = Z((a.y_start + a.y_end) / 2);
  const tint = a.kind === 'hot' ? tones.hot : a.kind === 'cold' ? tones.cold : tones.line;
  // Containment spans the rows that face the aisle, not the whole room.
  const flank = racks.filter((r) => Math.abs(r.y - (a.y_start + a.y_end) / 2) <= depth / 2 + (r.d_m ?? DEFAULT_D));
  const x0 = flank.length ? Math.min(...flank.map((r) => r.x - (r.w_m ?? DEFAULT_W) / 2)) : 0;
  const x1 = flank.length ? Math.max(...flank.map((r) => r.x + (r.w_m ?? DEFAULT_W) / 2)) : 0;
  const h = flank.length ? Math.max(...flank.map(rackH)) : 2;
  return (
    <group>
      <mesh rotation-x={-Math.PI / 2} position={[0, 0.004, zc]}>
        <planeGeometry args={[W, depth]} />
        <meshBasicMaterial color={tint} transparent opacity={0.22} depthWrite={false} />
      </mesh>
      {a.contained && flank.length > 0 && (
        <group>
          <mesh position={[X((x0 + x1) / 2), h + 0.01, zc]}>
            <boxGeometry args={[x1 - x0, 0.02, depth]} />
            <meshBasicMaterial color={tint} transparent opacity={0.14} depthWrite={false} />
          </mesh>
          {[x0, x1].map((xe) => (
            <mesh key={xe} position={[X(xe), h / 2, zc]}>
              <boxGeometry args={[0.02, h, depth]} />
              <meshBasicMaterial color={tint} transparent opacity={0.14} depthWrite={false} />
            </mesh>
          ))}
        </group>
      )}
    </group>
  );
}

function tipAt(e: ThreeEvent<PointerEvent>, text: string): Tip {
  return { x: e.nativeEvent.clientX, y: e.nativeEvent.clientY, text };
}

function Racks({ racks, X, Z, colors, sel, onSelect, onTip, tones }: {
  racks: FloorRack[]; X: (x: number) => number; Z: (y: number) => number;
  colors: THREE.Color[]; sel: Sel; onSelect: (s: Sel) => void; onTip: (t: Tip) => void; tones: Tones;
}) {
  const shell = useRef<THREE.InstancedMesh>(null);
  const cap = useRef<THREE.InstancedMesh>(null);
  const { invalidate } = useThree();

  useLayoutEffect(() => {
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), p = new THREE.Vector3(), s = new THREE.Vector3();
    racks.forEach((r, i) => {
      const w = r.w_m ?? DEFAULT_W, d = r.d_m ?? DEFAULT_D, h = rackH(r);
      shell.current?.setMatrixAt(i, m.compose(p.set(X(r.x), h / 2, Z(r.y)), q, s.set(w, h, d)));
      shell.current?.setColorAt(i, colors[i]);
      // An opaque lid in the overlay colour: from above, the rack reads at a glance.
      cap.current?.setMatrixAt(i, m.compose(p.set(X(r.x), h + 0.015, Z(r.y)), q, s.set(w, 0.03, d)));
      cap.current?.setColorAt(i, colors[i]);
    });
    for (const im of [shell.current, cap.current]) {
      if (!im) continue;
      im.instanceMatrix.needsUpdate = true;
      if (im.instanceColor) im.instanceColor.needsUpdate = true;
      im.computeBoundingSphere();
    }
    invalidate();
  }, [racks, colors, X, Z, invalidate]);

  // Rack frames and their intake edge, merged into two line sets.
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
        const fz = cz + (r.facing === 'N' ? -d : d);
        f.push(cx - w, 0.02, fz, cx + w, 0.02, fz, cx - w, h, fz, cx + w, h, fz);
      }
    }
    const ge = new THREE.BufferGeometry();
    ge.setAttribute('position', new THREE.Float32BufferAttribute(e, 3));
    const gf = new THREE.BufferGeometry();
    gf.setAttribute('position', new THREE.Float32BufferAttribute(f, 3));
    return [ge, gf];
  }, [racks, X, Z]);

  const label = (i: number) => {
    const r = racks[i];
    return [r.name, `${r.device_count} devices`,
      r.load_kw != null ? `${r.load_kw.toFixed(1)} kW` : null,
      r.max_inlet_c != null ? `inlet ${r.max_inlet_c.toFixed(1)} °C` : null,
      r.free_u != null ? `${r.free_u} U free` : null].filter(Boolean).join(' · ');
  };

  const handlers = {
    onClick: (e: ThreeEvent<MouseEvent>) => {
      e.stopPropagation();
      if (e.instanceId != null) onSelect({ kind: 'rack', id: racks[e.instanceId].id });
    },
    onPointerMove: (e: ThreeEvent<PointerEvent>) => {
      e.stopPropagation();
      if (e.instanceId != null) onTip(tipAt(e, label(e.instanceId)));
    },
    onPointerOut: () => onTip(null),
  };

  if (!racks.length) return null;
  return (
    <group>
      {/* Keyed by count: an instanced mesh cannot grow after it is built. */}
      <instancedMesh key={`s${racks.length}`} ref={shell} args={[undefined, undefined, racks.length]} {...handlers}>
        <boxGeometry args={[1, 1, 1]} />
        <meshStandardMaterial transparent opacity={sel ? 0.18 : 0.28} depthWrite={false} />
      </instancedMesh>
      <instancedMesh key={`c${racks.length}`} ref={cap} args={[undefined, undefined, racks.length]} {...handlers}>
        <boxGeometry args={[1, 1, 1]} />
        <meshStandardMaterial />
      </instancedMesh>
      <lineSegments geometry={edges}>
        <lineBasicMaterial color={tones.line} />
      </lineSegments>
      <lineSegments geometry={fronts}>
        <lineBasicMaterial color={tones.front} />
      </lineSegments>
    </group>
  );
}

/** Where a device's box sits: [x, y, z, width, height, depth] in scene metres. */
function deviceBox(d: TwinDevice, r: FloorRack, X: (x: number) => number, Z: (y: number) => number,
                   zeroIdx: number): [number, number, number, number, number, number] | null {
  const w = r.w_m ?? DEFAULT_W, dep = r.d_m ?? DEFAULT_D;
  const cx = X(r.x), cz = Z(r.y);
  const front = r.facing === 'N' ? -1 : 1;              // +z is south
  if (d.u_start != null && d.u_start > 0) {
    const h = d.u_height * U - 0.004;
    return [cx, RACK_BASE + (d.u_start - 1) * U + h / 2 + 0.002, cz, w - 0.08, h, dep - 0.12];
  }
  if (d.mount === 'zero_u') {
    // Vertical strips in the rear channel, one per side.
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

function RackDevices({ devices, rackById, X, Z, colors, onSelect, onTip }: {
  devices: TwinDevice[]; rackById: Map<string, FloorRack>;
  X: (x: number) => number; Z: (y: number) => number; colors: THREE.Color[];
  onSelect: (s: Sel) => void; onTip: (t: Tip) => void;
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

  useLayoutEffect(() => {
    const im = ref.current;
    if (!im) return;
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), p = new THREE.Vector3(), s = new THREE.Vector3();
    boxes.forEach((b, i) => {
      // An unplaceable device collapses to nothing rather than drawing at the origin.
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
                   onClick={(e) => {
                     e.stopPropagation();
                     if (e.instanceId != null) onSelect({ kind: 'device', id: devices[e.instanceId].id });
                   }}
                   onPointerMove={(e) => {
                     e.stopPropagation();
                     if (e.instanceId == null) return;
                     const d = devices[e.instanceId];
                     onTip(tipAt(e, [d.name, humanise(d.device_type),
                       d.u_start ? `U${d.u_start}` : d.mount, d.status.toLowerCase(),
                       d.max_severity !== 'CLEAR' ? d.max_severity.toLowerCase() : null]
                       .filter(Boolean).join(' · ')));
                   }}
                   onPointerOut={() => onTip(null)}>
      <boxGeometry args={[1, 1, 1]} />
      <meshStandardMaterial />
    </instancedMesh>
  );
}

function Equipment({ e, X, Z, color, selected, tones, onSelect, onTip }: {
  e: FloorEquipment; X: (x: number) => number; Z: (y: number) => number; color: THREE.Color;
  selected: boolean; tones: Tones; onSelect: (s: Sel) => void; onTip: (t: Tip) => void;
}) {
  const footprint = e.w_m != null && e.d_m != null;
  const w = e.w_m ?? 0.14, d = e.d_m ?? 0.14, h = e.h_m ?? 0.14;
  const y = footprint ? (e.mount === 'wall' ? (e.mount_height_m ?? h / 2) : h / 2)
    : (e.mount_height_m ?? 1.2);
  const edges = useMemo(() => new THREE.EdgesGeometry(new THREE.BoxGeometry(w, h, d)), [w, h, d]);
  const text = [e.name, humanise(e.device_type),
    footprint ? `${e.w_m} × ${e.d_m} × ${e.h_m} m` : e.mount,
    e.max_severity !== 'CLEAR' ? e.max_severity.toLowerCase() : null].filter(Boolean).join(' · ');
  return (
    // Local -z is the unit's front; rotating by -facing points it at the compass.
    <group position={[X(e.x as number), y, Z(e.y as number)]}
           rotation-y={-((e.facing_deg ?? 0) * Math.PI) / 180}
           onClick={(ev) => { ev.stopPropagation(); onSelect({ kind: 'equipment', id: e.id }); }}
           onPointerMove={(ev) => { ev.stopPropagation(); onTip(tipAt(ev, text)); }}
           onPointerOut={() => onTip(null)}>
      <mesh>
        <boxGeometry args={[w, h, d]} />
        <meshStandardMaterial color={color} transparent opacity={footprint ? 0.85 : 1} />
      </mesh>
      <lineSegments geometry={edges}>
        <lineBasicMaterial color={selected ? tones.accent : tones.line} />
      </lineSegments>
      {footprint && e.facing_deg != null && (
        <mesh position={[0, 0, -d / 2 - 0.006]}>
          <planeGeometry args={[w * 0.9, Math.min(h * 0.12, 0.12)]} />
          <meshBasicMaterial color={tones.front} side={THREE.DoubleSide} />
        </mesh>
      )}
    </group>
  );
}

/** The accent outline round the selected rack or device. A selected plant
 *  unit outlines itself (Equipment). */
function Highlight({ sel, rackById, devices, X, Z, color }: {
  sel: Sel; rackById: Map<string, FloorRack>; devices: TwinDevice[];
  X: (x: number) => number; Z: (y: number) => number; color: string;
}) {
  let box: [number, number, number, number, number, number] | null = null;
  if (sel?.kind === 'rack') {
    const r = rackById.get(sel.id);
    if (r) box = [X(r.x), rackH(r) / 2, Z(r.y), (r.w_m ?? DEFAULT_W) + 0.04, rackH(r) + 0.04, (r.d_m ?? DEFAULT_D) + 0.04];
  } else if (sel?.kind === 'device') {
    const d = devices.find((x) => x.id === sel.id);
    const r = d && rackById.get(d.rack_id);
    if (d && r) box = deviceBox(d, r, X, Z, 0);
  }
  const [bw, bh, bd] = box ? [box[3], box[4], box[5]] : [0, 0, 0];
  const geom = useMemo(() => new THREE.EdgesGeometry(new THREE.BoxGeometry(bw, bh, bd)), [bw, bh, bd]);
  if (!box) return null;
  return (
    <lineSegments geometry={geom} position={[box[0], box[1], box[2]]}>
      <lineBasicMaterial color={color} />
    </lineSegments>
  );
}

function SelectionCard({ data, sel }: { data: TwinRoomScene; sel: Sel }) {
  if (!sel) {
    return <p className="muted">Nothing selected.</p>;
  }
  if (sel.kind === 'rack') {
    const r = data.plan.racks.find((x) => x.id === sel.id);
    if (!r) return null;
    const inRack = data.devices.filter((d) => d.rack_id === r.id);
    const faults = inRack.filter((d) => d.max_severity !== 'CLEAR' || d.status === 'OFFLINE');
    return (
      <div className="room3d-card">
        <h4>Rack {r.name}</h4>
        <dl>
          <dt>Row</dt><dd>{r.row_name ?? '-'}{r.facing ? ` · faces ${r.facing === 'N' ? 'north' : 'south'}` : ''}</dd>
          <dt>Devices</dt><dd>{r.device_count}{r.offline_count ? ` · ${r.offline_count} offline` : ''}</dd>
          <dt>Load</dt><dd>{r.load_kw != null ? `${r.load_kw.toFixed(1)} kW` : '-'}
            {r.rated_power_kw ? ` of ${r.rated_power_kw} kW` : ''}</dd>
          <dt>Max inlet</dt><dd>{r.max_inlet_c != null ? `${r.max_inlet_c.toFixed(1)} °C` : 'no reading'}</dd>
          <dt>Free</dt><dd>{r.free_u != null ? `${r.free_u} of ${r.u_height ?? 42} U` : '-'}</dd>
        </dl>
        {faults.length > 0 && (
          <ul className="room3d-faults">
            {faults.slice(0, 6).map((d) => (
              <li key={d.id}><Link to={`/devices/${d.id}`}>{d.name}</Link>
                <span className="muted"> · {d.status === 'OFFLINE' ? 'offline' : d.max_severity.toLowerCase()}</span></li>
            ))}
          </ul>
        )}
        <Link to={`/racks/${r.id}?from=floorplan`}>Open rack elevation</Link>
      </div>
    );
  }
  const d = sel.kind === 'device'
    ? data.devices.find((x) => x.id === sel.id)
    : data.plan.equipment.find((x) => x.id === sel.id);
  if (!d) return null;
  return (
    <div className="room3d-card">
      <h4>{d.name}</h4>
      <dl>
        <dt>Type</dt><dd>{humanise(d.device_type)}</dd>
        <dt>Status</dt><dd>{d.status.toLowerCase()}{d.max_severity !== 'CLEAR' ? ` · ${d.max_severity.toLowerCase()}` : ''}</dd>
        {'u_start' in d && d.u_start != null && <><dt>Slot</dt><dd>U{d.u_start}{d.u_height > 1 ? `-${d.u_start + d.u_height - 1}` : ''}</dd></>}
        {'w_m' in d && d.w_m != null && <><dt>Footprint</dt><dd>{d.w_m} × {d.d_m} × {d.h_m} m{d.basis === 'class' ? ' (estimate)' : ''}</dd></>}
        {d.power_w != null && <><dt>Power</dt><dd>{(d.power_w / 1000).toFixed(2)} kW</dd></>}
      </dl>
      <Link to={`/devices/${d.id}`}>Open device</Link>
    </div>
  );
}
