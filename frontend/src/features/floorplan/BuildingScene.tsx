import { MapControls, OrbitControls, OrthographicCamera, PerspectiveCamera } from '@react-three/drei';
import { useThree, type ThreeEvent } from '@react-three/fiber';
import { useEffect, useMemo, useRef, type ElementRef } from 'react';
import * as THREE from 'three';
import type { TwinRoom, TwinSiteScene } from '../../api/client';
import { alarmColor, resolveColor } from './colors';
import { labelTexture, type HoverTip } from './Scene';

/**
 * The building rung (docs/27 §8b): a site's levels stacked in the same scene
 * the room viewer uses, at coarse detail. Slabs at their elevation, each room
 * as a translucent volume coloured by its worst open condition, its racks as
 * blocks and its floor-standing plant at true footprint. Nothing per device,
 * no air, no paths - those belong to the room, one click down.
 *
 * Coordinates: the import's site frame, metres, x east and y south on the
 * plan, elevation up. The scene is centred on the rooms' bounding box.
 */

export interface BuildingSceneProps {
  scene: TwinSiteScene;
  /** Focus one level: the others fade and stop answering the pointer. */
  level: string | null;
  mode: '3d' | 'plan';
  resetTick: number;
  themeTick: number;
  hover: string | null;
  onHover: (roomId: string | null) => void;
  onEnter: (roomId: string) => void;
  onTip: (t: HoverTip | null) => void;
}

const SLAB = 0.3;
const RACK_H = 2.0;
const RACK_W = 0.6, RACK_D = 1.2;
const DEFAULT_F2F = 5.0;

type Placed = TwinRoom & { origin_x_m: number; origin_y_m: number; width_m: number; depth_m: number };
const placed = (r: TwinRoom): r is Placed =>
  r.origin_x_m != null && r.origin_y_m != null && r.width_m != null && r.depth_m != null && r.width_m > 0 && r.depth_m > 0;

export default function BuildingScene(p: BuildingSceneProps) {
  const { scene, level, mode, resetTick, themeTick, hover, onHover, onEnter, onTip } = p;
  const { invalidate } = useThree();

  const tones = useMemo(() => ({
    bg: resolveColor('var(--bg-inset)'), ink: resolveColor('var(--text)'), inkMuted: resolveColor('var(--text-muted)'),
    faint: resolveColor('var(--text-faint)'), raised: resolveColor('var(--bg-raised)'),
    border: resolveColor('var(--border-strong)'), accent: resolveColor('var(--accent)'),
    slab: resolveColor('var(--border-strong)'),
    sev: (s: string) => resolveColor(alarmColor(s, 0)),
  }), [themeTick]);

  const levels = useMemo(() => [...scene.levels].sort((a, b) => a.ordinal - b.ordinal), [scene.levels]);
  const rooms = useMemo(() => scene.rooms.filter(placed), [scene.rooms]);
  const elevOf = (r: TwinRoom) => r.level_elevation_m ?? levels.find((l) => l.name === r.level)?.elevation_m ?? 0;
  const f2f = scene.floor_to_floor_m
    ?? (levels.length > 1 ? Math.min(...levels.slice(1).map((l, i) => l.elevation_m - levels[i].elevation_m)) : DEFAULT_F2F);
  const roomH = Math.max(2.4, f2f - SLAB - 0.2);

  // Frame: the rooms' bounding box, centred.
  const box = useMemo(() => {
    const pts = scene.outline_m?.length ? scene.outline_m.map(([x, y]) => [x, y] as const) : [];
    for (const r of rooms) pts.push([r.origin_x_m, r.origin_y_m], [r.origin_x_m + r.width_m, r.origin_y_m + r.depth_m]);
    if (!pts.length) return { x0: 0, y0: 0, x1: 10, y1: 10 };
    return { x0: Math.min(...pts.map((p) => p[0])), y0: Math.min(...pts.map((p) => p[1])),
             x1: Math.max(...pts.map((p) => p[0])), y1: Math.max(...pts.map((p) => p[1])) };
  }, [scene.outline_m, rooms]);
  const cx = (box.x0 + box.x1) / 2, cz = (box.y0 + box.y1) / 2;
  const X = (x: number) => x - cx, Z = (y: number) => y - cz;
  const span = Math.max(box.x1 - box.x0, box.y1 - box.y0, 8);
  const top = (levels.length ? levels[levels.length - 1].elevation_m : 0) + roomH;

  // In PLAN there is one level on the table: the focused one, else the one
  // with the most racks.
  const planLevel = level ?? rooms.reduce<{ name: string | null; n: number }>((best, r) => {
    const n = rooms.filter((x) => x.level === r.level).reduce((a, x) => a + x.rack_count, 0);
    return n > best.n ? { name: r.level ?? null, n } : best;
  }, { name: levels[0]?.name ?? null, n: -1 }).name;
  const shown = (lv: string | null | undefined) => (mode === 'plan' ? lv === planLevel : true);
  const dimmed = (lv: string | null | undefined) => (mode === '3d' && level != null && lv !== level);

  // Slab footprints: the site outline when it exists, else the level's rooms.
  const slabShapes = useMemo(() => levels.map((l) => {
    const rs = rooms.filter((r) => r.level === l.name);
    let pts: [number, number][];
    if (scene.outline_m && scene.outline_m.length >= 3) pts = scene.outline_m.map(([x, y]) => [X(x), Z(y)]);
    else if (rs.length) {
      const x0 = Math.min(...rs.map((r) => r.origin_x_m)), y0 = Math.min(...rs.map((r) => r.origin_y_m));
      const x1 = Math.max(...rs.map((r) => r.origin_x_m + r.width_m)), y1 = Math.max(...rs.map((r) => r.origin_y_m + r.depth_m));
      pts = [[X(x0), Z(y0)], [X(x1), Z(y0)], [X(x1), Z(y1)], [X(x0), Z(y1)]];
    } else return null;
    const shape = new THREE.Shape(pts.map(([x, z]) => new THREE.Vector2(x, z)));
    const geo = new THREE.ExtrudeGeometry(shape, { depth: SLAB, bevelEnabled: false });
    return { level: l, geo, rooms: rs.length };
  }).filter((s): s is NonNullable<typeof s> => s != null), [levels, rooms, scene.outline_m, cx, cz]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => slabShapes.forEach((s) => s.geo.dispose()), [slabShapes]);

  // Racks of the whole site as one instanced mesh, coloured by condition.
  const rackMesh = useRef<THREE.InstancedMesh | null>(null);
  const rackInstances = useMemo(() => {
    const out: { m: THREE.Matrix4; c: THREE.Color }[] = [];
    const tmp = new THREE.Object3D();
    for (const r of rooms) {
      if (!shown(r.level) || dimmed(r.level)) continue;
      const e = elevOf(r);
      for (const k of r.racks ?? []) {
        const w = k.w_m ?? RACK_W, d = k.d_m ?? RACK_D;
        tmp.position.set(X(r.origin_x_m + k.x), e + RACK_H / 2, Z(r.origin_y_m + k.y));
        tmp.scale.set(w, RACK_H, d);
        tmp.rotation.set(0, 0, 0);
        tmp.updateMatrix();
        out.push({ m: tmp.matrix.clone(), c: new THREE.Color(k.max_severity === 'CLEAR' ? tones.inkMuted : tones.sev(k.max_severity)) });
      }
    }
    return out;
  }, [rooms, level, mode, tones, cx, cz]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const m = rackMesh.current;
    if (!m) return;
    rackInstances.forEach((it, i) => { m.setMatrixAt(i, it.m); m.setColorAt(i, it.c); });
    m.count = rackInstances.length;
    m.instanceMatrix.needsUpdate = true;
    if (m.instanceColor) m.instanceColor.needsUpdate = true;
    invalidate();
  }, [rackInstances, invalidate]);

  useEffect(() => { invalidate(); }, [hover, level, mode, themeTick, invalidate]);

  const tip = (e: ThreeEvent<PointerEvent>, text: string) => onTip({ x: e.nativeEvent.clientX, y: e.nativeEvent.clientY, text });
  const roomText = (r: TwinRoom) => [r.name, r.level ? `level ${r.level}` : null,
    r.room_class === 'white_space' ? 'white space' : r.room_class === 'support' ? 'support room' : 'plant room',
    `${r.width_m} × ${r.depth_m} m`, r.rack_count ? `${r.rack_count} racks` : null, `${r.device_count} devices`,
    r.max_severity === 'CLEAR' ? 'no open alarms' : `${r.max_severity.toLowerCase()} alarm open`].filter(Boolean).join(' · ');

  return (
    <>
      <color attach="background" args={[tones.bg]} />
      <hemisphereLight args={['#ffffff', '#b9bec6', 0.9]} />
      <directionalLight position={[span * 0.6, top + 14, span * 0.4]} intensity={0.55} />
      <Cameras mode={mode} span={span} top={top} planElev={levels.find((l) => l.name === planLevel)?.elevation_m ?? 0}
               W={box.x1 - box.x0} D={box.y1 - box.y0} resetTick={resetTick} />

      {slabShapes.map(({ level: l, geo }) => shown(l.name) && (
        <mesh key={l.name} geometry={geo} rotation-x={Math.PI / 2} position={[0, l.elevation_m, 0]}>
          <meshStandardMaterial color={tones.slab} roughness={1} transparent opacity={dimmed(l.name) ? 0.1 : 0.75} />
        </mesh>
      ))}
      {slabShapes.map(({ level: l, geo }) => shown(l.name) && !dimmed(l.name) && (
        <lineSegments key={`e${l.name}`} rotation-x={Math.PI / 2} position={[0, l.elevation_m, 0]}>
          <edgesGeometry args={[geo]} />
          <lineBasicMaterial color={tones.faint} />
        </lineSegments>
      ))}

      {rooms.map((r) => shown(r.level) && (
        <RoomVolume key={r.id} room={r} e={elevOf(r)} h={roomH} X={X} Z={Z} tones={tones}
                    dim={dimmed(r.level)} hot={hover === r.id}
                    onOver={(ev) => { onHover(r.id); tip(ev, roomText(r)); }}
                    onOut={() => { onHover(null); onTip(null); }}
                    onClick={() => onEnter(r.id)} />
      ))}

      <instancedMesh ref={rackMesh} args={[undefined, undefined, Math.max(1, rackInstances.length)]} frustumCulled={false}>
        <boxGeometry args={[1, 1, 1]} />
        <meshStandardMaterial roughness={0.85} />
      </instancedMesh>

      {rooms.map((r) => shown(r.level) && !dimmed(r.level) && (r.equipment ?? []).map((u) => {
        const h = u.h_m ?? 1.5;
        return (
          <mesh key={u.id} position={[X(r.origin_x_m + u.x), elevOf(r) + h / 2, Z(r.origin_y_m + u.y)]}
                rotation-y={-((u.facing_deg ?? 0) * Math.PI) / 180}
                onPointerOver={(ev) => { ev.stopPropagation(); tip(ev, `${u.name} · ${u.device_type.replace(/_/g, ' ')} · ${u.w_m} × ${u.d_m} m${u.max_severity !== 'CLEAR' ? ` · ${u.max_severity.toLowerCase()}` : ''}`); }}
                onPointerOut={() => onTip(null)}>
            <boxGeometry args={[u.w_m, h, u.d_m]} />
            <meshStandardMaterial color={u.max_severity === 'CLEAR' ? tones.faint : tones.sev(u.max_severity)} roughness={0.9} />
          </mesh>
        );
      }))}
    </>
  );
}

function RoomVolume({ room: r, e, h, X, Z, tones, dim, hot, onOver, onOut, onClick }: {
  room: Placed; e: number; h: number; X: (x: number) => number; Z: (y: number) => number;
  tones: { ink: string; inkMuted: string; raised: string; accent: string; border: string; sev: (s: string) => string };
  dim: boolean; hot: boolean;
  onOver: (ev: ThreeEvent<PointerEvent>) => void; onOut: () => void; onClick: () => void;
}) {
  const colour = r.max_severity === 'CLEAR' ? tones.inkMuted : tones.sev(r.max_severity);
  const alarmed = r.max_severity !== 'CLEAR';
  const label = useMemo(() => labelTexture(r.name, tones.ink, tones.raised), [r.name, tones.ink, tones.raised]);
  useEffect(() => () => label.dispose(), [label]);
  const x = X(r.origin_x_m + r.width_m / 2), z = Z(r.origin_y_m + r.depth_m / 2);
  const geo = useMemo(() => new THREE.BoxGeometry(r.width_m, h, r.depth_m), [r.width_m, h, r.depth_m]);
  useEffect(() => () => geo.dispose(), [geo]);
  return (
    <group position={[x, e + h / 2, z]}>
      <mesh geometry={geo} onPointerOver={dim ? undefined : (ev) => { ev.stopPropagation(); onOver(ev); }}
            onPointerOut={dim ? undefined : onOut} onClick={dim ? undefined : (ev) => { ev.stopPropagation(); onClick(); }}
            raycast={dim ? () => null : undefined}>
        <meshStandardMaterial color={colour} transparent depthWrite={false}
                              opacity={dim ? 0.03 : hot ? 0.38 : alarmed ? 0.3 : 0.16} roughness={1} />
      </mesh>
      <lineSegments>
        <edgesGeometry args={[geo]} />
        <lineBasicMaterial color={hot ? tones.accent : alarmed ? colour : tones.inkMuted} transparent opacity={dim ? 0.15 : 0.9} />
      </lineSegments>
      {!dim && (
        <sprite position={[0, h / 2 + 0.6, 0]} renderOrder={10} scale={[Math.min(4, r.width_m * 0.6), Math.min(4, r.width_m * 0.6) / 4, 1]}>
          <spriteMaterial map={label} depthTest={false} transparent />
        </sprite>
      )}
    </group>
  );
}

function Cameras({ mode, span, top, planElev, W, D, resetTick }: {
  mode: '3d' | 'plan'; span: number; top: number; planElev: number; W: number; D: number; resetTick: number;
}) {
  const orbit = useRef<ElementRef<typeof OrbitControls>>(null);
  const map = useRef<ElementRef<typeof MapControls>>(null);
  const { camera, size, invalidate } = useThree();
  useEffect(() => {
    if (mode === '3d') {
      camera.up.set(0, 1, 0);
      camera.position.set(span * 0.95, top + span * 0.55, span * 1.15);
      orbit.current?.target.set(0, top / 2, 0);
      orbit.current?.update();
    } else {
      camera.position.set(0, planElev + 60, 0);
      camera.up.set(0, 0, -1);
      camera.lookAt(0, planElev, 0);
      const cam = camera as THREE.OrthographicCamera;
      if (cam.isOrthographicCamera) {
        cam.zoom = Math.min(size.width / (W + 4), size.height / (D + 4));
        cam.updateProjectionMatrix();
      }
      map.current?.target.set(0, planElev, 0);
      map.current?.update();
    }
    invalidate();
  }, [mode, resetTick, camera, span, top, planElev, W, D, size.width, size.height, invalidate]);
  if (mode === 'plan') {
    return (
      <>
        <OrthographicCamera makeDefault near={0.1} far={500} position={[0, 60, 0]} />
        <MapControls ref={map} makeDefault enableRotate={false} screenSpacePanning minZoom={3} maxZoom={200} />
      </>
    );
  }
  return (
    <>
      <PerspectiveCamera makeDefault fov={45} near={0.1} far={800} />
      <OrbitControls ref={orbit} makeDefault enableDamping={false} maxPolarAngle={Math.PI / 2.05}
                     minDistance={3} maxDistance={span * 5} />
    </>
  );
}
