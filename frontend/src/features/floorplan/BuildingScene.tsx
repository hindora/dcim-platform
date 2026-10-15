import type { Spin } from './ViewBar';
import { useDolly, useQuarterTurn } from './Scene';
import { MapControls, OrbitControls, OrthographicCamera, PerspectiveCamera } from '@react-three/drei';
import { useThree, type ThreeEvent } from '@react-three/fiber';
import { useEffect, useMemo, useRef, useState, type ElementRef } from 'react';
import * as THREE from 'three';
import { renderToStaticMarkup } from 'react-dom/server';
import type { AlarmCategory, TwinDoor, TwinRoom, TwinSiteScene, TwinSpace } from '../../api/client';
import { CategoryGlyph } from '../../components/CategoryGlyph';
import { alarmColor, resolveColor } from './colors';
import { NO_READING, bandColor, roomLayerBands, roomLayerText, roomLayerValue, type BuildingLayer } from './layers';
import { labelTexture, type GBox, type HoverTip } from './Scene';
import { Hardware } from './Hardware';
import { Fence, Ground, ZoneObject, ZoneSurface } from './SiteDressing';

/**
 * The site and building rungs (docs/27 §8b, §8c), drawn in the same scene
 * the room viewer uses, at coarse detail.
 *
 * Site rung: the fenced compound as a ground plane with its zones flat and
 * grey (road, apron, substation, tanks, pump house, telecom entrances), the
 * outdoor rooms with their equipment at true footprint, and the building as a
 * translucent shell over its levels - click it to go in.
 *
 * Building rung: level slabs at their elevation with the indoor fabric
 * (corridor, core, lobby, dock, door openings) flat on them, each room a
 * translucent volume coloured by its worst open condition, its racks as
 * blocks and its plant at true footprint. Nothing per device, no air, no
 * paths - those belong to the room, one click down.
 *
 * Fabric and zones carry no state and are never coloured by it. Coordinates
 * are the import's plan frame, metres, x east and y south, elevation up; the
 * building's bounding box is the origin at both rungs so the camera moves
 * between them without the world shifting.
 */

export type Rung = 'site' | 'building';

export interface BuildingSceneProps {
  scene: TwinSiteScene;
  rung: Rung;
  /** Focus one level (building rung): the others fade and stop answering the pointer. */
  level: string | null;
  /** What colours a room: its worst open condition, or a figure of its racks. */
  layer: BuildingLayer;
  mode: '3d' | 'plan';
  /** Building rung, 3D: the floors pulled apart (eased), to see into each one. */
  split?: boolean;
  resetTick: number;
  spin?: Spin;
  themeTick: number;
  hover: string | null;
  onHover: (roomId: string | null) => void;
  onEnter: (roomId: string) => void;
  onEnterBuilding: () => void;
  onTip: (t: HoverTip | null) => void;
}

const SLAB = 0.3;
const RACK_H = 2.0;
const RACK_W = 0.6, RACK_D = 1.2;
const DEFAULT_F2F = 5.0;
/** The building rung in 3D: each floor a frosted-glass tray, the racks and
 *  plant charcoal inside it, on a dark studio. Physical colours, the same in
 *  both themes, like the room's SURF. */
const GLASS = {
  slab: '#aeb9c7', rim: '#dfe7f0', wall: '#c8d3df', room: '#b9c4d1', roomEdge: '#cfd8e3',
  rack: '#2a2f36', plant: '#3b4148', glowIn: '#1d2532', glowOut: '#090c11',
};
/** How far a split pulls each floor from the one below, as a share of the floor-to-floor height. */
const SPLIT_GAP = 0.9;
/** The hardware pass reads geometry only; a block's paint is the instanced colour. */
const NO_PAINT = { bot: '#000000', mid: '#000000', top: '#000000', fill: 1 };
const OUTDOOR = 'site';
/** Zones worth a name on the ground; the rest are read from their shape. */
const LABELLED_ZONES = new Set(['substation', 'generator_yard', 'cooling_yard', 'fuel_tank', 'water_tank',
                                'fire_pump_house', 'guardhouse', 'parking', 'apron', 'gate']);
const ZONE_WORD: Record<string, string> = {
  generator_yard: 'generator yard', fuel_tank: 'bulk fuel', water_tank: 'makeup water', fire_pump_house: 'fire pumps',
  telecom_entrance: 'telecom entrance', cooling_yard: 'cooling yard', driveway: 'driveway',
};

type Placed = TwinRoom & { origin_x_m: number; origin_y_m: number; width_m: number; depth_m: number };
const placed = (r: TwinRoom): r is Placed =>
  r.origin_x_m != null && r.origin_y_m != null && r.width_m != null && r.depth_m != null && r.width_m > 0 && r.depth_m > 0;
const isOutdoor = (lv: string | null | undefined) => (lv ?? '').toLowerCase() === OUTDOOR;
const bboxOf = (poly: number[][]) => ({
  x0: Math.min(...poly.map((p) => p[0])), y0: Math.min(...poly.map((p) => p[1])),
  x1: Math.max(...poly.map((p) => p[0])), y1: Math.max(...poly.map((p) => p[1])),
});

export default function BuildingScene(p: BuildingSceneProps) {
  const { scene, rung, layer, level, mode, split = false, resetTick, spin, themeTick, hover, onHover, onEnter, onEnterBuilding, onTip } = p;
  const { invalidate } = useThree();
  const site = rung === 'site';

  const tones = useMemo(() => ({
    bg: resolveColor('var(--bg-inset)'), ink: resolveColor('var(--text)'), inkMuted: resolveColor('var(--text-muted)'),
    faint: resolveColor('var(--text-faint)'), raised: resolveColor('var(--bg-raised)'),
    border: resolveColor('var(--border)'), accent: resolveColor('var(--accent)'),
    slab: resolveColor('var(--border-strong)'),
    sev: (s: string) => resolveColor(alarmColor(s, 0)),
    none: resolveColor(NO_READING),
  }), [themeTick]);
  // A room's colour under the chosen layer: alarm severity, or its figure's
  // band; a room with nothing to measure keeps the "no reading" tone so it
  // is not mistaken for a cool one.
  const roomColour = (r: TwinRoom): { colour: string; lit: boolean; band?: boolean } => {
    // An alarm is a marker on the rack or unit that raised it (AlarmPins), never a red room.
    if (layer === 'alarm') return { colour: tones.inkMuted, lit: false };
    const v = roomLayerValue(r, layer);
    if (v == null) return { colour: tones.none, lit: false };
    return { colour: resolveColor(bandColor(roomLayerBands(layer)!, v)), lit: true, band: true };
  };

  const levels = useMemo(() => [...scene.levels].sort((a, b) => a.ordinal - b.ordinal), [scene.levels]);
  const allRooms = useMemo(() => scene.rooms.filter(placed), [scene.rooms]);
  // Indoor rooms belong to the building; outdoor rooms stand in the compound
  // and are drawn at the site rung only.
  const rooms = useMemo(() => allRooms.filter((r) => !isOutdoor(r.level)), [allRooms]);
  const yards = useMemo(() => (site ? allRooms.filter((r) => isOutdoor(r.level)) : []), [allRooms, site]);
  const baseElev = (r: TwinRoom) => (isOutdoor(r.level) ? 0 : r.level_elevation_m ?? levels.find((l) => l.name === r.level)?.elevation_m ?? 0);
  const f2f = scene.floor_to_floor_m
    ?? (levels.length > 1 ? Math.min(...levels.slice(1).map((l, i) => l.elevation_m - levels[i].elevation_m)) : DEFAULT_F2F);
  const roomH = Math.max(2.4, f2f - SLAB - 0.2);

  // The split, eased: t runs 0 (stacked) to 1 (apart) over ~0.45 s, at once
  // under reduced motion. Each level rises by its index times the gap.
  // The glass studio is the 3D view at both rungs: the compound draws the
  // building exactly as the building view does, with the yards around it.
  const glass = mode === '3d';
  const want = glass && split && !site ? 1 : 0;
  const [t, setT] = useState(want);
  useEffect(() => {
    if (t === want) return undefined;
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) { setT(want); return undefined; }
    const from = t, t0 = performance.now();
    let raf = 0;
    const step = (now: number) => {
      const k = Math.min(1, (now - t0) / 450), e = k < 0.5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
      setT(from + (want - from) * e);
      invalidate();
      if (k < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [want]); // eslint-disable-line react-hooks/exhaustive-deps
  const gap = f2f * SPLIT_GAP;
  const levelIdx = useMemo(() => new Map(levels.map((l, i) => [l.name, i])), [levels]);
  const lift = (lv: string | null | undefined) => (isOutdoor(lv) ? 0 : t * (levelIdx.get(lv ?? '') ?? 0) * gap);
  const elevOf = (r: TwinRoom) => baseElev(r) + lift(r.level);

  // Frame: the building's bounding box is the origin at both rungs.
  const box = useMemo(() => {
    const pts = scene.outline_m?.length ? scene.outline_m.map(([x, y]) => [x, y] as const) : [];
    for (const r of rooms) pts.push([r.origin_x_m, r.origin_y_m], [r.origin_x_m + r.width_m, r.origin_y_m + r.depth_m]);
    if (!pts.length) return { x0: 0, y0: 0, x1: 10, y1: 10 };
    return { x0: Math.min(...pts.map((q) => q[0])), y0: Math.min(...pts.map((q) => q[1])),
             x1: Math.max(...pts.map((q) => q[0])), y1: Math.max(...pts.map((q) => q[1])) };
  }, [scene.outline_m, rooms]);
  const cx = (box.x0 + box.x1) / 2, cz = (box.y0 + box.y1) / 2;
  const X = (x: number) => x - cx, Z = (y: number) => y - cz;
  const compound = scene.compound ?? null;
  const ground = useMemo(() => {
    const poly = compound?.outline_m?.length ? compound.outline_m : null;
    return poly ? bboxOf(poly) : { x0: box.x0 - 12, y0: box.y0 - 10, x1: box.x1 + 12, y1: box.y1 + 10 };
  }, [compound, box]);
  const frame = site ? ground : box;
  const span = Math.max(frame.x1 - frame.x0, frame.y1 - frame.y0, 8);
  const top = (levels.length ? levels[levels.length - 1].elevation_m : 0) + roomH;
  // The camera frames the stack it is going to: the split one when asked for.
  const camTop = top + (want ? Math.max(0, levels.length - 1) * gap : 0);
  // The structure stops at the roof slab: a level named for the roof is
  // open air, and the plant on it stands in the weather, not in a glass box.
  const roofElev = levels.find((l) => /roof/i.test(l.name))?.elevation_m ?? top;

  // In PLAN there is one level on the table: at the site rung the ground, at
  // the building rung the focused level, else the one with the most racks.
  const planLevel = site ? (levels[0]?.name ?? null) : level ?? rooms.reduce<{ name: string | null; n: number }>((best, r) => {
    const n = rooms.filter((x) => x.level === r.level).reduce((a, x) => a + x.rack_count, 0);
    return n > best.n ? { name: r.level ?? null, n } : best;
  }, { name: levels[0]?.name ?? null, n: -1 }).name;
  const shown = (lv: string | null | undefined) => (mode === 'plan' ? lv === planLevel : true);
  const dimmed = (lv: string | null | undefined) => (!site && mode === '3d' && level != null && lv !== level);

  // A flat polygon in the plan frame, as a shape geometry lying on the ground.
  const flat = (poly: number[][]) => {
    const shape = new THREE.Shape(poly.map(([x, y]) => new THREE.Vector2(X(x), Z(y))));
    return new THREE.ShapeGeometry(shape);
  };

  // Slab footprints: the building outline when it exists, else the level's rooms.
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

  // Ground, zones and fabric as flat geometries, rebuilt only when the data does.
  const groundGeo = useMemo(() => (site ? flat([[ground.x0, ground.y0], [ground.x1, ground.y0], [ground.x1, ground.y1], [ground.x0, ground.y1]]) : null),
    [site, ground, cx, cz]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => groundGeo?.dispose(), [groundGeo]);
  const zoneGeos = useMemo(() => (site && compound ? compound.zones
    .filter((z) => z.polygon_m.length >= 3 && z.kind !== 'fence' && !z.room_id)
    .map((z) => ({ z, geo: flat(z.polygon_m), bb: bboxOf(z.polygon_m) })) : []),
    [site, compound, cx, cz]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => zoneGeos.forEach((g) => g.geo.dispose()), [zoneGeos]);
  const fencePoly = useMemo(() => {
    const f = site ? compound?.zones.find((z) => z.kind === 'fence') : null;
    return f?.polygon_m?.length ? f.polygon_m : compound?.outline_m ?? null;
  }, [site, compound]);
  const fence = useMemo(() => {
    const poly = fencePoly;
    if (!site || !poly) return null;
    const pts = [...poly, poly[0]].map(([x, y]) => new THREE.Vector3(X(x), 0.03, Z(y)));
    return new THREE.BufferGeometry().setFromPoints(pts);
  }, [site, compound, cx, cz]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => fence?.dispose(), [fence]);
  // The contacts, by the room whose opening they watch. A room with one door
  // and one contact is every room the simulator exports; a second contact on
  // the same room would take the first here, which is still that room's door.
  const doorsByRoom = useMemo(() => {
    const m = new Map<string, TwinDoor>();
    for (const d of scene.doors ?? []) if (d.room_id && !m.has(d.room_id)) m.set(d.room_id, d);
    return m;
  }, [scene.doors]);
  const doorOf = (f: TwinSpace) => (f.room_id ? doorsByRoom.get(f.room_id) : undefined);
  const doorText = (f: TwinSpace, d: TwinDoor | undefined) => {
    const name = f.name ?? 'Door';
    if (!d) return `${name}${f.attrs?.monitored ? ' · monitored by the BMS, no contact reading yet' : ' · opening, drawing only'}`;
    const state = d.open == null ? 'contact not reporting' : d.open ? 'open' : 'closed';
    const sev = d.max_severity !== 'CLEAR' ? ` · ${d.max_severity.toLowerCase()} alarm open` : '';
    return `${name} · ${state}${sev}`;
  };
  const fabricGeos = useMemo(() => (!site ? (scene.fabric ?? [])
    .filter((f) => f.polygon_m.length >= 3)
    .map((f) => ({ f, geo: f.kind === 'door' || f.kind === 'entrance' ? null : flat(f.polygon_m), bb: bboxOf(f.polygon_m),
                   elev: levels.find((l) => l.name === f.level)?.elevation_m ?? 0 })) : []),
    [site, scene.fabric, levels, cx, cz]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => fabricGeos.forEach((g) => g.geo?.dispose()), [fabricGeos]);
  const shellGeo = useMemo(() => new THREE.BoxGeometry(box.x1 - box.x0, roofElev, box.y1 - box.y0), [box, roofElev]);
  // The compound's dressing: where the gate breaks the fence.
  const gateBB = useMemo(() => { const g = compound?.zones.find((z) => z.kind === 'gate'); return g && g.polygon_m.length >= 3 ? bboxOf(g.polygon_m) : null; }, [compound]);
  useEffect(() => () => shellGeo.dispose(), [shellGeo]);

  // Racks of the whole site as one instanced mesh, coloured by condition.
  const rackMesh = useRef<THREE.InstancedMesh | null>(null);
  const rackInstances = useMemo(() => {
    const out: { m: THREE.Matrix4; c: THREE.Color }[] = [];
    const tmp = new THREE.Object3D();
    for (const r of [...rooms, ...yards]) {
      if (!isOutdoor(r.level) && (!shown(r.level) || dimmed(r.level))) continue;
      const e = elevOf(r);
      for (const k of r.racks ?? []) {
        const w = k.w_m ?? RACK_W, d = k.d_m ?? RACK_D;
        tmp.position.set(X(r.origin_x_m + k.x), e + RACK_H / 2, Z(r.origin_y_m + k.y));
        tmp.scale.set(w, RACK_H, d);
        tmp.rotation.set(0, 0, 0);
        tmp.updateMatrix();
        out.push({ m: tmp.matrix.clone(), c: new THREE.Color(glass ? GLASS.rack : tones.inkMuted) });
      }
    }
    return out;
  }, [rooms, yards, level, mode, tones, cx, cz, t]); // eslint-disable-line react-hooks/exhaustive-deps
  // The physical detail the room view gives cabinets and plant (Hardware.tsx),
  // at building scale: the racks' steel frame, the plant's pads, nozzles,
  // stacks, fins and handles. Same filters as the blocks themselves.
  const hw = useMemo(() => {
    const racksB: GBox[] = [], unitsB: GBox[] = [], types: string[] = [];
    for (const r of [...rooms, ...yards]) {
      const indoor = !isOutdoor(r.level);
      if (indoor && (!shown(r.level) || dimmed(r.level))) continue;
      const e = elevOf(r);
      for (const k of r.racks ?? []) {
        racksB.push({ x: X(r.origin_x_m + k.x), y: e + RACK_H / 2, z: Z(r.origin_y_m + k.y),
                      w: k.w_m ?? RACK_W, h: RACK_H, d: k.d_m ?? RACK_D, rot: 0, front: 0, paint: NO_PAINT });
      }
      for (const u of r.equipment ?? []) {
        const h = u.h_m ?? 1.5;
        unitsB.push({ x: X(r.origin_x_m + u.x), y: e + h / 2, z: Z(r.origin_y_m + u.y), w: u.w_m, h, d: u.d_m,
                      rot: -((u.facing_deg ?? 0) * Math.PI) / 180, front: 0, paint: NO_PAINT });
        types.push(u.device_type);
      }
    }
    return { racks: racksB, units: unitsB, types };
  }, [rooms, yards, level, mode, cx, cz, t]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const m = rackMesh.current;
    if (!m) return;
    rackInstances.forEach((it, i) => { m.setMatrixAt(i, it.m); m.setColorAt(i, it.c); });
    m.count = rackInstances.length;
    m.instanceMatrix.needsUpdate = true;
    if (m.instanceColor) m.instanceColor.needsUpdate = true;
    invalidate();
  }, [rackInstances, invalidate]);

  useEffect(() => { invalidate(); }, [hover, level, mode, rung, themeTick, invalidate]);

  const tip = (e: ThreeEvent<PointerEvent>, text: string) => onTip({ x: e.nativeEvent.clientX, y: e.nativeEvent.clientY, text });
  const roomText = (r: TwinRoom) => [r.name, isOutdoor(r.level) ? 'in the compound' : r.level ? `level ${r.level}` : null,
    layer !== 'alarm' ? roomLayerText(r, layer) : null,
    r.room_class === 'white_space' ? 'white space' : r.room_class === 'support' ? 'support room'
      : r.room_class === 'outdoor' ? 'outdoor' : 'plant room',
    `${r.width_m} × ${r.depth_m} m`, r.rack_count ? `${r.rack_count} racks` : null, `${r.device_count} devices`,
    r.max_severity === 'CLEAR' ? 'no open alarms' : `${r.max_severity.toLowerCase()} alarm open`].filter(Boolean).join(' · ');
  const zoneText = (z: TwinSpace) => `${z.name ?? ZONE_WORD[z.kind] ?? z.kind.replace(/_/g, ' ')} · ${ZONE_WORD[z.kind] ?? z.kind.replace(/_/g, ' ')} · drawing, no sensors`;
  const buildingHot = hover === '__building__';
  const buildingText = `${scene.name} · ${levels.length} levels · ${rooms.length} rooms · click to enter`;

  // Alarm markers: one over each alarmed rack and unit, and one at the room's
  // centre when its alarm comes from something not drawn up here (a wall
  // sensor, a door contact), so no open alarm goes unseen.
  const pins = useMemo(() => {
    const out: Pin[] = [];
    for (const r of [...rooms, ...yards]) {
      if (!isOutdoor(r.level) && (!shown(r.level) || dimmed(r.level))) continue;
      const e = elevOf(r);
      let own = false;
      for (const k of r.racks ?? []) {
        if (k.max_severity === 'CLEAR') continue;
        out.push({ x: X(r.origin_x_m + k.x), y: e + RACK_H + 0.45, z: Z(r.origin_y_m + k.y), c: tones.sev(k.max_severity),
                   cat: k.alarm_category ?? null, n: k.alarm_count ?? 1 });
        own = true;
      }
      for (const u of r.equipment ?? []) {
        if (u.max_severity === 'CLEAR') continue;
        out.push({ x: X(r.origin_x_m + u.x), y: e + (u.h_m ?? 1.5) + 0.45, z: Z(r.origin_y_m + u.y), c: tones.sev(u.max_severity),
                   cat: u.alarm_category ?? null, n: u.alarm_count ?? 1 });
        own = true;
      }
      if (!own && r.max_severity !== 'CLEAR') {
        out.push({ x: X(r.origin_x_m + r.width_m / 2), y: e + 1.6, z: Z(r.origin_y_m + r.depth_m / 2), c: tones.sev(r.max_severity),
                   cat: r.alarm_category ?? null, n: r.alarm_count ?? 1 });
      }
    }
    return out;
  }, [rooms, yards, level, mode, tones, cx, cz, t]); // eslint-disable-line react-hooks/exhaustive-deps

  const unitBoxes = (r: Placed) => (r.equipment ?? []).map((u) => {
    const h = u.h_m ?? 1.5;
    return (
      <mesh key={u.id} position={[X(r.origin_x_m + u.x), elevOf(r) + h / 2, Z(r.origin_y_m + u.y)]}
            rotation-y={-((u.facing_deg ?? 0) * Math.PI) / 180}
            onPointerOver={(ev) => { ev.stopPropagation(); tip(ev, `${u.name} · ${u.device_type.replace(/_/g, ' ')} · ${u.w_m} × ${u.d_m} m${u.max_severity !== 'CLEAR' ? ` · ${u.max_severity.toLowerCase()}` : ''}`); }}
            onPointerOut={() => onTip(null)}>
        <boxGeometry args={[u.w_m, h, u.d_m]} />
        <meshStandardMaterial color={glass ? GLASS.plant : tones.faint} roughness={glass ? 0.6 : 0.9} />
      </mesh>
    );
  });

  return (
    <>
      {glass ? <Studio /> : <color attach="background" args={[tones.bg]} />}
      <hemisphereLight args={['#eef2f6', '#7a8089', 0.85]} />
      <directionalLight position={[-span * 0.5, top + 20, -span * 0.35]} intensity={0.75} />
      <directionalLight position={[span * 0.6, top + 8, span * 0.5]} intensity={0.25} />
      {site && mode === '3d' && <fog attach="fog" args={[GLASS.glowOut, span * 1.8, span * 4.5]} />}
      <Cameras mode={mode} span={span} top={site ? top * 0.6 : camTop} planElev={site ? 0 : levels.find((l) => l.name === planLevel)?.elevation_m ?? 0}
               W={frame.x1 - frame.x0} D={frame.y1 - frame.y0} cx={X((frame.x0 + frame.x1) / 2)} cz={Z((frame.y0 + frame.y1) / 2)}
               resetTick={resetTick} spin={spin} rung={rung} />

      {/* --- the compound (site rung) --- */}
      {groundGeo && <Ground ground={ground} X={X} Z={Z} faint={tones.faint} />}
      {fence && (
        <>
          <line>
            <primitive object={fence} attach="geometry" />
            <lineBasicMaterial color={tones.inkMuted} />
          </line>
          {fencePoly && <Fence poly={fencePoly} gate={gateBB} X={X} Z={Z} ink={tones.inkMuted} />}
        </>
      )}
      {zoneGeos.map(({ z, geo, bb }) => (
        <group key={z.id}>
          <mesh geometry={geo} rotation-x={Math.PI / 2} position={[0, 0.01, 0]}
                onPointerOver={(ev) => { ev.stopPropagation(); tip(ev, zoneText(z)); }} onPointerOut={() => onTip(null)}>
            <meshStandardMaterial color={glass ? GLASS.slab : tones.slab} roughness={1} transparent
                                  opacity={z.kind === 'road' || z.kind === 'driveway' || z.kind === 'apron' || z.kind === 'parking' || z.kind === 'telecom_entrance' ? 0.0
                                    : z.kind === 'gate' ? 0.2 : 0.5}
                                  side={THREE.DoubleSide} depthWrite={false} />
          </mesh>
          <ZoneSurface z={z} bb={bb} X={X} Z={Z} />
          <ZoneObject z={z} bb={bb} X={X} Z={Z} ink={tones.inkMuted} />
          {!glass && LABELLED_ZONES.has(z.kind) && (
            <Label text={z.name ?? ZONE_WORD[z.kind] ?? z.kind} ink={tones.inkMuted} bg={tones.raised}
                   x={X((bb.x0 + bb.x1) / 2)} y={0.4} z={Z((bb.y0 + bb.y1) / 2)} w={Math.min(5, Math.max(2.5, (bb.x1 - bb.x0) * 0.6))} />
          )}
        </group>
      ))}
      {yards.map((r) => (
        <group key={r.id}>
          <RoomVolume room={r} e={0} h={0.25} X={X} Z={Z} tones={tones} dim={false} hot={hover === r.id} flat glass={glass} paint={roomColour(r)}
                      onOver={(ev) => { onHover(r.id); tip(ev, roomText(r)); }}
                      onOut={() => { onHover(null); onTip(null); }}
                      onClick={() => onEnter(r.id)} />
          {unitBoxes(r)}
        </group>
      ))}

      {/* --- the building --- */}
      {slabShapes.map(({ level: l, geo }) => shown(l.name) && (
        <mesh key={l.name} geometry={geo} rotation-x={Math.PI / 2} position={[0, l.elevation_m + lift(l.name), 0]}>
          {/* Under a figure's layer the slabs thin out: the colour that matters is on the rooms
              beneath them, and a stack of 75 % slabs greys every level but the top one. In the
              building's 3D view a slab is frosted glass. */}
          {glass
            ? <meshStandardMaterial color={GLASS.slab} emissive={GLASS.slab} emissiveIntensity={0.18} roughness={0.4}
                                    transparent depthWrite={false} opacity={dimmed(l.name) ? 0.06 : 0.26} />
            : <meshStandardMaterial color={tones.slab} roughness={1} transparent
                                    opacity={dimmed(l.name) ? 0.1 : layer !== 'alarm' ? (site ? 0.22 : 0.3) : site ? 0.55 : 0.75} />}
        </mesh>
      ))}
      {slabShapes.map(({ level: l, geo }) => shown(l.name) && !dimmed(l.name) && (
        <lineSegments key={`e${l.name}`} rotation-x={Math.PI / 2} position={[0, l.elevation_m + lift(l.name), 0]}>
          <edgesGeometry args={[geo]} />
          <lineBasicMaterial color={glass ? GLASS.rim : tones.faint} transparent={glass} opacity={glass ? 0.85 : 1} />
        </lineSegments>
      ))}
      {glass && slabShapes.map(({ level: l, geo }) => (
        <GlassTray key={`g${l.name}`} geo={geo} y={l.elevation_m + lift(l.name)}
                   h={/roof/i.test(l.name) ? 1.1 : roomH} dim={dimmed(l.name)} />
      ))}
      {fabricGeos.map(({ f, geo, bb, elev: e0 }) => { const elev = e0 + lift(f.level); return shown(f.level) && !dimmed(f.level) && (
        <group key={f.id}>
          {geo ? (
            <mesh geometry={geo} rotation-x={Math.PI / 2} position={[0, elev + 0.02, 0]}
                  onPointerOver={(ev) => { ev.stopPropagation(); tip(ev, `${f.name ?? f.kind} · level ${f.level} · drawing, no sensors`); }}
                  onPointerOut={() => onTip(null)}>
              {/* In the glass view the corridor and lobby are a faint frosting on the slab, not a dark fill. */}
              <meshStandardMaterial color={glass ? GLASS.slab : tones.raised} roughness={1} transparent opacity={glass ? 0.12 : 0.85}
                                    side={THREE.DoubleSide} depthWrite={false} />
            </mesh>
          ) : (
            // A door opening: a short bar on the wall. With a contact on it the bar
            // is the leaf - it swings open when the contact says so, and a held-open
            // alarm colours it.
            <DoorLeaf f={f} bb={bb} elev={elev} door={doorOf(f)} X={X} Z={Z} tones={tones}
                      onOver={(ev) => { ev.stopPropagation(); tip(ev, doorText(f, doorOf(f))); }}
                      onOut={() => onTip(null)} />
          )}
        </group>
      ); })}

      {rooms.map((r) => shown(r.level) && (
        <RoomVolume key={r.id} room={r} e={elevOf(r)} h={roomH} X={X} Z={Z} tones={tones} paint={roomColour(r)}
                    dim={dimmed(r.level)} hot={hover === r.id} quiet={site} glass={glass}
                    onOver={(ev) => { onHover(r.id); tip(ev, roomText(r)); }}
                    onOut={() => { onHover(null); onTip(null); }}
                    onClick={() => onEnter(r.id)} />
      ))}

      <instancedMesh ref={rackMesh} args={[undefined, undefined, Math.max(1, rackInstances.length)]} frustumCulled={false}>
        <boxGeometry args={[1, 1, 1]} />
        <meshStandardMaterial roughness={glass ? 0.55 : 0.85} />
      </instancedMesh>

      {rooms.map((r) => shown(r.level) && !dimmed(r.level) && unitBoxes(r))}
      <RackDoors boxes={hw.racks} />
      <Hardware racks={hw.racks} units={hw.units} unitTypes={hw.types} open={false} />
      <AlarmPins pins={pins} />

      {/* The building's shell: at the site rung it is the thing you click. */}
      {site && mode === '3d' && (
        <group position={[0, roofElev / 2, 0]}>
          <mesh geometry={shellGeo} onPointerOver={(ev) => { ev.stopPropagation(); onHover('__building__'); tip(ev, buildingText); }}
                onPointerOut={() => { onHover(null); onTip(null); }}
                onClick={(ev) => { ev.stopPropagation(); onEnterBuilding(); }}>
            <meshStandardMaterial color={tones.inkMuted} transparent opacity={0.02} depthWrite={false} roughness={1} />
          </mesh>
          {/* The glass floors are the building; its outline only answers a hover. */}
          {buildingHot && (
            <lineSegments>
              <edgesGeometry args={[shellGeo]} />
              <lineBasicMaterial color={tones.accent} />
            </lineSegments>
          )}
        </group>
      )}
      {site && mode === 'plan' && (
        <mesh geometry={shellGeo} position={[0, roofElev / 2, 0]} onClick={(ev) => { ev.stopPropagation(); onEnterBuilding(); }}
              onPointerOver={(ev) => { ev.stopPropagation(); onHover('__building__'); tip(ev, buildingText); }}
              onPointerOut={() => { onHover(null); onTip(null); }}>
          <meshBasicMaterial transparent opacity={buildingHot ? 0.08 : 0.001} depthWrite={false} />
        </mesh>
      )}
    </>
  );
}

function Label({ text, ink, bg, x, y, z, w }: { text: string; ink: string; bg: string; x: number; y: number; z: number; w: number }) {
  const tex = useMemo(() => labelTexture(text, ink, bg), [text, ink, bg]);
  useEffect(() => () => tex.dispose(), [tex]);
  return (
    <sprite position={[x, y, z]} renderOrder={10} scale={[w, w / 4, 1]}>
      <spriteMaterial map={tex} depthTest={false} transparent />
    </sprite>
  );
}

function RoomVolume({ room: r, e, h, X, Z, tones, paint, dim, hot, quiet, flat, glass, onOver, onOut, onClick }: {
  room: Placed; e: number; h: number; X: (x: number) => number; Z: (y: number) => number;
  tones: { ink: string; inkMuted: string; raised: string; accent: string; border: string; sev: (s: string) => string };
  /** The room's colour under the current layer, and whether it is a reading rather than a resting tone.
   *  `band` marks a figure's band: an alarm tint stands out against grey neighbours, but under a figure
   *  every measured room is coloured and the band has to be read, so the faces carry more of it. */
  paint: { colour: string; lit: boolean; band?: boolean };
  dim: boolean; hot: boolean;
  /** At the site rung the rooms sit inside the shell: no labels, lower alpha. */
  quiet?: boolean;
  /** An outdoor room: a footprint on the ground, not a volume. */
  flat?: boolean;
  /** The building's glass view: a room at rest is a glass partition; its name is in the hover tooltip, not a label. */
  glass?: boolean;
  onOver: (ev: ThreeEvent<PointerEvent>) => void; onOut: () => void; onClick: () => void;
}) {
  const alarmed = paint.lit;
  // At rest in the glass view a room is clear glass; a reading still tints it.
  const resting = !!glass && !alarmed && !paint.band;
  const colour = resting ? GLASS.room : paint.colour;
  const label = useMemo(() => labelTexture(r.name, tones.ink, tones.raised), [r.name, tones.ink, tones.raised]);
  useEffect(() => () => label.dispose(), [label]);
  const x = X(r.origin_x_m + r.width_m / 2), z = Z(r.origin_y_m + r.depth_m / 2);
  const geo = useMemo(() => new THREE.BoxGeometry(r.width_m, h, r.depth_m), [r.width_m, h, r.depth_m]);
  useEffect(() => () => geo.dispose(), [geo]);
  const inert = dim || quiet;
  return (
    <group position={[x, e + h / 2, z]}>
      <mesh geometry={geo} onPointerOver={inert ? undefined : (ev) => { ev.stopPropagation(); onOver(ev); }}
            onPointerOut={inert ? undefined : onOut} onClick={inert ? undefined : (ev) => { ev.stopPropagation(); onClick(); }}
            raycast={inert ? () => null : undefined}>
        <meshStandardMaterial color={colour} emissive={paint.band ? colour : '#000000'} emissiveIntensity={paint.band ? 0.7 : 0}
                              transparent depthWrite={false}
                              opacity={dim ? 0.03 : paint.band ? (hot ? 0.62 : quiet ? 0.34 : 0.48)
                                : resting ? (hot ? 0.16 : 0.05)
                                : hot ? 0.38 : quiet ? (alarmed ? 0.22 : 0.08) : alarmed ? 0.3 : flat ? 0.22 : 0.16} roughness={1} />
      </mesh>
      <lineSegments>
        <edgesGeometry args={[geo]} />
        <lineBasicMaterial color={hot ? tones.accent : alarmed ? colour : resting ? GLASS.roomEdge : tones.inkMuted} transparent
                           opacity={dim ? 0.15 : quiet ? 0.5 : resting && !hot ? 0.35 : 0.9} />
      </lineSegments>
      {/* Only the compound's yards carry a name on the ground; a room in the building is
          named by its hover tooltip and the Rooms list. */}
      {flat && !dim && !quiet && !glass && (
        <sprite position={[0, h / 2 + 0.6, 0]} renderOrder={10} scale={[Math.min(4, r.width_m * 0.6), Math.min(4, r.width_m * 0.6) / 4, 1]}>
          <spriteMaterial map={label} depthTest={false} transparent />
        </sprite>
      )}
    </group>
  );
}

/** The leaf of a door opening. Closed it lies in the wall; open it stands
 *  at 75 degrees into the room, hinged at one end, the way a plan drawing
 *  shows a door. Ink when the contact reads closed, accent when open, the
 *  alarm colour when held open long enough to be one. */
function DoorLeaf({ f, bb, elev, door, X, Z, tones, onOver, onOut }: {
  f: TwinSpace; bb: { x0: number; y0: number; x1: number; y1: number }; elev: number;
  door: TwinDoor | undefined; X: (x: number) => number; Z: (y: number) => number;
  tones: { ink: string; faint: string; accent: string; sev: (s: string) => string };
  onOver: (ev: ThreeEvent<PointerEvent>) => void; onOut: () => void;
}) {
  const w = Math.max(0.2, bb.x1 - bb.x0), d = Math.max(0.2, bb.y1 - bb.y0);
  const alongX = w >= d;                       // the opening runs east-west
  const len = alongX ? w : d;
  const open = door?.open === true;
  const colour = !door ? (f.attrs?.monitored ? tones.ink : tones.faint)
    : door.max_severity !== 'CLEAR' ? tones.sev(door.max_severity)
    : open ? tones.accent : tones.ink;
  // Hinge at the opening's west (or north) end. Closed, the leaf lies along
  // the wall from the hinge: east, or south. Open, it swings into the room:
  // the corridor runs along the south side of every level, so a door in an
  // east-west wall opens north, and a door in a west wall opens east. (An
  // entrance in the north wall would swing out; the fabric has none.)
  // three.js: rotation-y of +a turns local +x toward -z, which is north.
  const swing = open ? (75 * Math.PI) / 180 : 0;
  return (
    <group position={[X(alongX ? bb.x0 : (bb.x0 + bb.x1) / 2), elev + 0.08, Z(alongX ? (bb.y0 + bb.y1) / 2 : bb.y0)]}
           rotation-y={alongX ? swing : -Math.PI / 2 + swing}>
      <mesh position={[len / 2, 0, 0]} onPointerOver={onOver} onPointerOut={onOut}>
        <boxGeometry args={[len, open ? 0.6 : 0.16, 0.16]} />
        <meshStandardMaterial color={colour} roughness={1} />
      </mesh>
    </group>
  );
}

/** A rack door seen from the aisle: dark perforated steel in a frame, the
 *  handle, and a few lit status LEDs on the gear behind it. Front and back
 *  both get one - the building does not know which way a rack faces. */
function doorTexture(): THREE.CanvasTexture {
  const W = 64, H = 192;
  const cv = document.createElement('canvas');
  cv.width = W; cv.height = H;
  const c = cv.getContext('2d') as CanvasRenderingContext2D;
  c.fillStyle = '#1f242b'; c.fillRect(0, 0, W, H);
  c.fillStyle = '#2b323b';
  for (let y = 10; y < H - 8; y += 4) for (let x = 7 + ((y / 4) % 2) * 2; x < W - 6; x += 4) c.fillRect(x, y, 2, 2);
  c.strokeStyle = '#3c4550'; c.lineWidth = 3; c.strokeRect(1.5, 1.5, W - 3, H - 3);
  c.fillStyle = '#8a939d'; c.fillRect(W - 9, H * 0.45, 3, 18);
  const leds = [[14, 30], [22, 30], [14, 62], [30, 94], [14, 126], [22, 158]];
  for (const [x, y] of leds) { c.fillStyle = '#3a8dde'; c.fillRect(x, y, 3, 2); }
  c.fillStyle = '#3ddc84'; c.fillRect(38, 30, 2, 2); c.fillRect(38, 94, 2, 2);
  const t = new THREE.CanvasTexture(cv);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}

function RackDoors({ boxes }: { boxes: GBox[] }) {
  const ref = useRef<THREE.InstancedMesh>(null);
  const tex = useMemo(doorTexture, []);
  useEffect(() => () => tex.dispose(), [tex]);
  const n = boxes.length * 2;
  useEffect(() => {
    const im = ref.current;
    if (!im) return;
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), p = new THREE.Vector3(), sc = new THREE.Vector3();
    const up = new THREE.Vector3(0, 1, 0);
    boxes.forEach((b, i) => {
      for (const [k, sign] of [[0, 1], [1, -1]] as const) {
        q.setFromAxisAngle(up, sign > 0 ? 0 : Math.PI);
        im.setMatrixAt(i * 2 + k, m.compose(p.set(b.x, b.y, b.z + sign * (b.d / 2 + 0.003)), q, sc.set(b.w * 0.94, b.h * 0.96, 1)));
      }
    });
    im.instanceMatrix.needsUpdate = true;
    im.computeBoundingSphere();
  }, [boxes]);
  if (!n) return null;
  return (
    <instancedMesh key={n} ref={ref} args={[undefined, undefined, n]} frustumCulled={false} raycast={() => null}>
      <planeGeometry args={[1, 1]} />
      <meshBasicMaterial map={tex} />
    </instancedMesh>
  );
}

interface Pin { x: number; y: number; z: number; c: string; cat: AlarmCategory | null; n: number }

/** One alarm badge as an image: a disc in the severity colour, the category's
 *  glyph in white (the same icons as Home's indicator strip; a bell when the
 *  category is not known), a dark ring so it reads on the white tile and the
 *  dark studio alike, and the count in a bubble when more than one is open. */
function badgeTexture(cat: AlarmCategory | null, colour: string, n: number, onReady: () => void): THREE.CanvasTexture {
  const S = 128;
  const cv = document.createElement('canvas');
  cv.width = cv.height = S;
  const c = cv.getContext('2d') as CanvasRenderingContext2D;
  const tex = new THREE.CanvasTexture(cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  const disc = () => {
    c.clearRect(0, 0, S, S);
    c.fillStyle = colour; c.beginPath(); c.arc(S / 2, S / 2, 46, 0, Math.PI * 2); c.fill();
    c.strokeStyle = 'rgba(10,12,16,0.9)'; c.lineWidth = 6; c.stroke();
  };
  const count = () => {
    if (n <= 1) return;
    const t = n > 9 ? '9+' : String(n);
    c.fillStyle = '#0d1117'; c.beginPath(); c.arc(S - 26, 26, 22, 0, Math.PI * 2); c.fill();
    c.strokeStyle = '#ffffff'; c.lineWidth = 3; c.stroke();
    c.fillStyle = '#ffffff'; c.font = 'bold 24px system-ui, sans-serif'; c.textAlign = 'center'; c.textBaseline = 'middle';
    c.fillText(t, S - 26, 27);
  };
  disc(); count();
  const svg = renderToStaticMarkup(<CategoryGlyph kind={cat ?? 'alarms'} size={56} />)
    .replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg" color="#ffffff" stroke-width="1.6"');
  const img = new Image();
  img.onload = () => { disc(); c.drawImage(img, S / 2 - 28, S / 2 - 28, 56, 56); count(); tex.needsUpdate = true; onReady(); };
  img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  return tex;
}

/** Alarm markers: a badge over whatever raised it - severity by colour, what
 *  kind of alarm by its icon. The same size on screen at any distance and
 *  drawn over the glass, so a far-off alarm is never lost. The badges pulse
 *  (1.2 s, a soft fade, not a flash), and hold still under reduced motion; the
 *  scene only repaints while a badge is on screen. */
function AlarmPins({ pins }: { pins: Pin[] }) {
  const group = useRef<THREE.Group>(null);
  const { invalidate } = useThree();
  const textures = useMemo(() => new Map<string, THREE.CanvasTexture>(), []);
  useEffect(() => () => { textures.forEach((t) => t.dispose()); textures.clear(); }, [textures]);
  const texFor = (p: Pin) => {
    const key = `${p.cat ?? 'alarms'}|${p.c}|${Math.min(p.n, 10)}`;
    let t = textures.get(key);
    if (!t) { t = badgeTexture(p.cat, p.c, p.n, invalidate); textures.set(key, t); }
    return t;
  };
  useEffect(() => {
    if (!pins.length || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return undefined;
    const t0 = performance.now();
    const id = window.setInterval(() => {
      const k = 0.62 + 0.38 * Math.cos(((performance.now() - t0) / 1200) * Math.PI * 2);
      group.current?.children.forEach((o) => { ((o as THREE.Sprite).material as THREE.SpriteMaterial).opacity = k; });
      invalidate();
    }, 60);
    return () => window.clearInterval(id);
  }, [pins.length, invalidate]);
  return (
    <group ref={group}>
      {pins.map((p, i) => (
        <sprite key={i} position={[p.x, p.y, p.z]} renderOrder={20} scale={[0.04, 0.04, 1]} raycast={() => null}>
          <spriteMaterial map={texFor(p)} sizeAttenuation={false} depthTest={false} transparent />
        </sprite>
      ))}
    </group>
  );
}

/** The glass view's backdrop: a dark navy studio with a soft glow behind the building. */
function Studio() {
  const { scene, invalidate } = useThree();
  useEffect(() => {
    const cv = document.createElement('canvas');
    cv.width = cv.height = 512;
    const c = cv.getContext('2d') as CanvasRenderingContext2D;
    const g = c.createRadialGradient(256, 230, 20, 256, 256, 380);
    g.addColorStop(0, GLASS.glowIn); g.addColorStop(1, GLASS.glowOut);
    c.fillStyle = g; c.fillRect(0, 0, 512, 512);
    const tex = new THREE.CanvasTexture(cv);
    tex.colorSpace = THREE.SRGBColorSpace;
    scene.background = tex;
    invalidate();
    return () => { if (scene.background === tex) scene.background = null; tex.dispose(); };
  }, [scene, invalidate]);
  return null;
}

/** A floor's glass sides: the slab's outline raised to the ceiling, brightest
 *  at the floor and fading upward, with a faint rim along the top. */
function GlassTray({ geo, y, h, dim }: { geo: THREE.ExtrudeGeometry; y: number; h: number; dim: boolean }) {
  const { walls, rim } = useMemo(() => {
    geo.computeBoundingBox();
    const b = geo.boundingBox as THREE.Box3;
    const w = b.max.x - b.min.x, d = b.max.y - b.min.y;
    return { walls: { w, d, cx: (b.max.x + b.min.x) / 2, cz: (b.max.y + b.min.y) / 2 },
             rim: new THREE.EdgesGeometry(new THREE.BoxGeometry(w, h, d)) };
  }, [geo, h]);
  useEffect(() => () => rim.dispose(), [rim]);
  const alpha = useMemo(() => {
    const cv = document.createElement('canvas');
    cv.width = 4; cv.height = 128;
    const c = cv.getContext('2d') as CanvasRenderingContext2D;
    const g = c.createLinearGradient(0, 128, 0, 0);
    g.addColorStop(0, '#5a5a5a'); g.addColorStop(0.25, '#262626'); g.addColorStop(0.92, '#121212'); g.addColorStop(1, '#3a3a3a');
    c.fillStyle = g; c.fillRect(0, 0, 4, 128);
    return new THREE.CanvasTexture(cv);
  }, []);
  useEffect(() => () => alpha.dispose(), [alpha]);
  const mats = useMemo(() => {
    const side = new THREE.MeshBasicMaterial({ color: GLASS.wall, alphaMap: alpha, transparent: true, opacity: dim ? 0.25 : 1,
                                               depthWrite: false, side: THREE.DoubleSide });
    const none = new THREE.MeshBasicMaterial({ visible: false });
    return [side, side, none, none, side, side];
  }, [alpha, dim]);
  useEffect(() => () => { mats[0].dispose(); mats[2].dispose(); }, [mats]);
  return (
    <group position={[walls.cx, y + h / 2, walls.cz]}>
      <mesh material={mats} raycast={() => null}>
        <boxGeometry args={[walls.w, h, walls.d]} />
      </mesh>
      <lineSegments geometry={rim} raycast={() => null}>
        <lineBasicMaterial color={GLASS.rim} transparent opacity={dim ? 0.12 : 0.45} />
      </lineSegments>
    </group>
  );
}

function Cameras({ mode, span, top, planElev, W, D, cx, cz, resetTick, spin, rung }: {
  mode: '3d' | 'plan'; span: number; top: number; planElev: number; W: number; D: number; cx: number; cz: number;
  resetTick: number; spin?: Spin; rung: Rung;
}) {
  const orbit = useRef<ElementRef<typeof OrbitControls>>(null);
  const map = useRef<ElementRef<typeof MapControls>>(null);
  const { camera, size, invalidate } = useThree();
  useQuarterTurn(spin, orbit, camera, invalidate);
  const dolly = useDolly(orbit, camera, invalidate);
  useEffect(() => {
    if (mode === '3d') {
      camera.up.set(0, 1, 0);
      orbit.current?.target.set(cx, top / 2, cz);
      if (rung === 'building') {
        // Frame the whole stack: back off until its bounding sphere fits the 45 degree view.
        const r = 0.5 * Math.hypot(W, D, top), dist = (r / Math.sin((22.5 * Math.PI) / 180)) * 1.15;
        const dir = new THREE.Vector3(0.62, 0.42, 0.66).normalize().multiplyScalar(dist);
        dolly(new THREE.Vector3(cx + dir.x, top / 2 + dir.y, cz + dir.z), new THREE.Vector3(cx, top / 2, cz));
      } else {
        dolly(new THREE.Vector3(cx + span * 0.8, top + span * 0.5, cz + span * 0.95), new THREE.Vector3(cx, top / 2, cz));
      }
      orbit.current?.update();
    } else {
      camera.position.set(cx, planElev + 60, cz);
      camera.up.set(0, 0, -1);
      camera.lookAt(cx, planElev, cz);
      const cam = camera as THREE.OrthographicCamera;
      if (cam.isOrthographicCamera) {
        cam.zoom = Math.min(size.width / (W + 4), size.height / (D + 4));
        cam.updateProjectionMatrix();
      }
      map.current?.target.set(cx, planElev, cz);
      map.current?.update();
    }
    invalidate();
  }, [mode, rung, resetTick, camera, span, top, planElev, W, D, cx, cz, size.width, size.height, invalidate, dolly]);
  if (mode === 'plan') {
    return (
      <>
        <OrthographicCamera makeDefault near={0.1} far={500} position={[0, 60, 0]} />
        <MapControls ref={map} makeDefault enableRotate={false} screenSpacePanning minZoom={2} maxZoom={200} />
      </>
    );
  }
  return (
    <>
      <PerspectiveCamera makeDefault fov={45} near={0.1} far={1000} />
      <OrbitControls ref={orbit} makeDefault enableDamping={false} maxPolarAngle={Math.PI / 2.05}
                     minDistance={3} maxDistance={span * 5} />
    </>
  );
}
