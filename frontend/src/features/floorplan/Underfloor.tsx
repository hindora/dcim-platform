import { useEffect, useLayoutEffect, useMemo, useRef } from 'react';
import * as THREE from 'three';
import type { FloorAisle, FloorEquipment, FloorRack } from '../../api/client';
import { TILE } from './layers';

/**
 * The raised floor of a white-space room and what lives under it.
 *
 * A raised floor is 600 mm tiles on steel pedestals over the structural slab;
 * the void is the supply-air plenum the downflow air handlers blow into and the
 * perforated tiles let up. The import carries no void depth, so it is the
 * common 600 mm (real floors run 450-900 mm) - assumed, like the vent tiles.
 *
 * The deck's cut edge is always drawn, so the room reads as raised. "See under
 * the floor" opens the void. Nothing in it is reported by a device; it is drawn
 * where the standards put it, the way the fire points are:
 * - a pedestal at every tile corner, on the slab;
 * - power cable trays low under the cold aisles, one whip up to each rack, and
 *   data trays higher under the hot aisles - the separation TIA-942 asks for
 *   when both run under the floor;
 * - a chilled-water supply and return header under each wall of air handlers,
 *   a riser up into each unit, and a leak-detection rope round the run.
 */

export const PLENUM_H = 0.6;

const C = {
  slab: '#8e959c', voidInk: '#262b31', tileEdge: '#e8ebee', pedestal: '#b9bfc5',
  tray: '#aab1b8', power: '#30353b', data: '#3b8ed8', pipe: '#e1e4e8', supplyBand: '#2f7fd1',
  returnBand: '#d64541', rope: '#e8c22c', whip: '#30353b',
};

/** One tile-wide strip of the deck's cut edge: the tile's thickness, the void,
 *  and the pedestal at the tile corner with its head and base plates. */
function sectionTexture(repeat: number): THREE.CanvasTexture {
  const cv = document.createElement('canvas');
  cv.width = 64; cv.height = 64;
  const c = cv.getContext('2d') as CanvasRenderingContext2D;
  c.fillStyle = C.voidInk; c.fillRect(0, 0, 64, 64);
  c.fillStyle = C.tileEdge; c.fillRect(0, 0, 64, 4);                 // the tile, ~35 mm
  c.fillStyle = C.pedestal;
  c.fillRect(0, 4, 3, 60); c.fillRect(61, 4, 3, 60);                 // the corner pedestal, split across the seam
  c.fillRect(0, 4, 7, 2); c.fillRect(57, 4, 7, 2);                   // head plate
  c.fillRect(0, 61, 8, 3); c.fillRect(56, 61, 8, 3);                 // base plate
  const t = new THREE.CanvasTexture(cv);
  t.colorSpace = THREE.SRGBColorSpace;
  t.wrapS = THREE.RepeatWrapping; t.repeat.set(Math.max(1, repeat), 1);
  return t;
}

/** The deck seen from outside: four cut faces round the room, under its tiles. */
export function DeckEdge({ W, D }: { W: number; D: number }) {
  const mats = useMemo(() => {
    const along = sectionTexture(Math.round(W / TILE)), across = sectionTexture(Math.round(D / TILE));
    const face = (map: THREE.Texture) => new THREE.MeshBasicMaterial({ map });
    const none = new THREE.MeshBasicMaterial({ visible: false });
    // BoxGeometry faces: +x, -x, +y, -y, +z, -z
    return [face(across), face(across), none, none, face(along), face(along)];
  }, [W, D]);
  useEffect(() => () => mats.forEach((m) => { (m as THREE.MeshBasicMaterial).map?.dispose(); m.dispose(); }), [mats]);
  return (
    <mesh position={[0, -PLENUM_H / 2 - 0.001, 0]} material={mats} raycast={() => null}>
      <boxGeometry args={[W, PLENUM_H, D]} />
    </mesh>
  );
}

type Seg = [number, number, number, number, number, number];

/** The opened void: slab, pedestals, trays, whips, chilled-water headers. */
export function Plenum({ W, D, X, Z, racks, aisles, units }: {
  W: number; D: number; X: (x: number) => number; Z: (y: number) => number;
  racks: FloorRack[]; aisles: FloorAisle[]; units: FloorEquipment[];
}) {
  const floor = -PLENUM_H;

  const pedestals = useMemo(() => {
    const out: [number, number][] = [];
    const cols = Math.round(W / TILE), rows = Math.round(D / TILE);
    for (let i = 0; i <= cols; i++) for (let j = 0; j <= rows; j++) out.push([-W / 2 + i * TILE, -D / 2 + j * TILE]);
    return out;
  }, [W, D]);

  // Trays: one per aisle that has racks on it, spanning the racks either side.
  const trays = useMemo(() => aisles.filter((a) => a.kind === 'cold' || a.kind === 'hot').map((a) => {
    const mid = (a.y_start + a.y_end) / 2, depth = a.y_end - a.y_start;
    const flank = racks.filter((r) => Math.abs(r.y - mid) <= depth / 2 + (r.d_m ?? 1.2));
    if (!flank.length) return null;
    const x0 = Math.min(...flank.map((r) => r.x - (r.w_m ?? 0.6) / 2));
    const x1 = Math.max(...flank.map((r) => r.x + (r.w_m ?? 0.6) / 2));
    const power = a.kind === 'cold';
    return { power, x: X((x0 + x1) / 2), len: x1 - x0, z: Z(mid), y: power ? floor + 0.08 : -0.2 };
  }).filter((t): t is NonNullable<typeof t> => t != null), [aisles, racks, X, Z, floor]);

  // Whips: each rack drops from the power tray in the cold aisle its front opens onto.
  const whips = useMemo(() => {
    const segs: Seg[] = [];
    const cold = trays.filter((t) => t.power);
    for (const r of racks) {
      if (!cold.length) break;
      const rz = Z(r.y);
      const t = cold.reduce((b, c) => (Math.abs(c.z - rz) < Math.abs(b.z - rz) ? c : b));
      const x = X(r.x), y = t.y + 0.04;
      segs.push([x, y, t.z, x, y, rz], [x, y, rz, x, -0.04, rz]);
    }
    return segs;
  }, [racks, trays, X, Z]);

  // Chilled water: a header pair under each wall's line of air handlers.
  const water = useMemo(() => {
    const crahs = units.filter((e) => (e.device_type === 'crah' || e.device_type === 'crac') && e.mount !== 'wall'
      && e.x != null && e.y != null);
    const byWall = new Map<string, FloorEquipment[]>();
    for (const e of crahs) {
      const x = e.x as number, y = e.y as number;
      const k = Math.min(x, W - x) <= Math.min(y, D - y) ? (x < W / 2 ? 'W' : 'E') : (y < D / 2 ? 'N' : 'S');
      byWall.set(k, [...(byWall.get(k) ?? []), e]);
    }
    const runs: { a: THREE.Vector3; b: THREE.Vector3 }[] = [];
    const risers: THREE.Vector3[] = [];
    const branches: { a: THREE.Vector3; b: THREE.Vector3 }[] = [];
    const rope: Seg[] = [];
    for (const [k, list] of byWall) {
      const alongZ = k === 'W' || k === 'E';
      const inward = (k === 'W' || k === 'N') ? 1 : -1;
      // The units' line, then the header pair just in front of them (a unit is
      // installed long side to the wall, so its short side is its depth).
      const line = alongZ ? list.reduce((s, e) => s + (e.x as number), 0) / list.length
                          : list.reduce((s, e) => s + (e.y as number), 0) / list.length;
      const half = Math.max(...list.map((e) => Math.min(e.w_m ?? 0.9, e.d_m ?? 0.9) / 2));
      const fixed = line + inward * (half + 0.2);
      const ts = list.map((e) => (alongZ ? e.y : e.x) as number);
      const t0 = Math.min(...ts) - 0.6, t1 = Math.max(...ts) + 0.6;
      [0, 0.28].forEach((off, i) => {
        const f = fixed + inward * off, y = floor + 0.16 + i * 0.02;
        const a = alongZ ? new THREE.Vector3(X(f), y, Z(t0)) : new THREE.Vector3(X(t0), y, Z(f));
        const b = alongZ ? new THREE.Vector3(X(f), y, Z(t1)) : new THREE.Vector3(X(t1), y, Z(f));
        runs.push({ a, b });
        // a branch back under each unit, and the riser up into its coil
        for (const e of list) {
          const t = (alongZ ? e.y : e.x) as number, tt = t + (i ? 0.2 : -0.2);
          const P = (g: number) => (alongZ ? new THREE.Vector3(X(g), y, Z(tt)) : new THREE.Vector3(X(tt), y, Z(g)));
          branches.push({ a: P(f), b: P(line) });
          risers.push(P(line));
        }
      });
      // leak-detection rope on the slab, a loop round the header pair
      const lo = fixed - (inward > 0 ? 0.25 : 0.53), hi = fixed + (inward > 0 ? 0.53 : 0.25), y = floor + 0.006;
      const P = (f: number, t: number): [number, number, number] =>
        (alongZ ? [X(f), y, Z(t)] : [X(t), y, Z(f)]);
      const corners = [P(lo, t0), P(hi, t0), P(hi, t1), P(lo, t1)];
      for (let i = 0; i < 4; i++) rope.push([...corners[i], ...corners[(i + 1) % 4]] as Seg);
    }
    return { runs, risers, branches, rope };
  }, [units, W, D, X, Z, floor]);

  const lines = (segs: Seg[]) => {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(segs.flat(), 3));
    return g;
  };
  const whipGeo = useMemo(() => lines(whips), [whips]);
  const ropeGeo = useMemo(() => lines(water.rope), [water.rope]);
  useEffect(() => () => { whipGeo.dispose(); ropeGeo.dispose(); }, [whipGeo, ropeGeo]);

  return (
    <group>
      <mesh rotation-x={-Math.PI / 2} position={[0, floor, 0]} raycast={() => null}>
        <planeGeometry args={[W, D]} />
        <meshBasicMaterial color={C.slab} />
      </mesh>
      <Pedestals at={pedestals} floor={floor} />

      {trays.map((t, i) => (
        <group key={i} position={[t.x, t.y, t.z]}>
          {/* wire basket: a floor and two rails; the cable bundle inside */}
          <mesh raycast={() => null}><boxGeometry args={[t.len, 0.006, 0.3]} />
            <meshStandardMaterial color={C.tray} metalness={0.1} roughness={0.6} /></mesh>
          {[-0.15, 0.15].map((dz) => (
            <mesh key={dz} position={[0, 0.03, dz]} raycast={() => null}><boxGeometry args={[t.len, 0.06, 0.006]} />
              <meshStandardMaterial color={C.tray} metalness={0.1} roughness={0.6} /></mesh>
          ))}
          <mesh position={[0, 0.03, 0]} raycast={() => null}><boxGeometry args={[t.len - 0.06, 0.05, 0.24]} />
            <meshStandardMaterial color={t.power ? C.power : C.data} roughness={0.8} /></mesh>
        </group>
      ))}
      <lineSegments geometry={whipGeo} raycast={() => null}><lineBasicMaterial color={C.whip} /></lineSegments>

      {water.runs.map(({ a, b }, i) => {
        const len = a.distanceTo(b), mid = a.clone().add(b).multiplyScalar(0.5);
        const alongZ = Math.abs(a.z - b.z) > Math.abs(a.x - b.x);
        return (
          <group key={i} position={mid}>
            <mesh rotation={alongZ ? [Math.PI / 2, 0, 0] : [0, 0, Math.PI / 2]} raycast={() => null}>
              <cylinderGeometry args={[0.06, 0.06, len, 14]} />
              <meshStandardMaterial color={C.pipe} roughness={0.5} />
            </mesh>
            {/* a coloured band at each end: supply blue, return red */}
            {[-1, 1].map((s) => (
              <mesh key={s} position={alongZ ? [0, 0, s * (len / 2 - 0.15)] : [s * (len / 2 - 0.15), 0, 0]}
                    rotation={alongZ ? [Math.PI / 2, 0, 0] : [0, 0, Math.PI / 2]} raycast={() => null}>
                <cylinderGeometry args={[0.064, 0.064, 0.08, 14]} />
                <meshStandardMaterial color={i % 2 ? C.returnBand : C.supplyBand} roughness={0.5} />
              </mesh>
            ))}
          </group>
        );
      })}
      {water.branches.map(({ a, b }, i) => {
        const len = a.distanceTo(b), mid = a.clone().add(b).multiplyScalar(0.5);
        const alongZ = Math.abs(a.z - b.z) > Math.abs(a.x - b.x);
        return (
          <mesh key={`b${i}`} position={mid} rotation={alongZ ? [Math.PI / 2, 0, 0] : [0, 0, Math.PI / 2]} raycast={() => null}>
            <cylinderGeometry args={[0.035, 0.035, len, 10]} />
            <meshStandardMaterial color={C.pipe} roughness={0.5} />
          </mesh>
        );
      })}
      {water.risers.map((p, i) => (
        <mesh key={i} position={[p.x, p.y + (-p.y) / 2, p.z]} raycast={() => null}>
          <cylinderGeometry args={[0.035, 0.035, -p.y, 10]} />
          <meshStandardMaterial color={C.pipe} roughness={0.5} />
        </mesh>
      ))}
      <lineSegments geometry={ropeGeo} raycast={() => null}><lineBasicMaterial color={C.rope} /></lineSegments>
    </group>
  );
}

function Pedestals({ at, floor }: { at: [number, number][]; floor: number }) {
  const stem = useRef<THREE.InstancedMesh>(null), plate = useRef<THREE.InstancedMesh>(null);
  useLayoutEffect(() => {
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), p = new THREE.Vector3(), s = new THREE.Vector3();
    at.forEach(([x, z], i) => {
      stem.current?.setMatrixAt(i, m.compose(p.set(x, floor / 2, z), q, s.set(0.035, -floor - 0.03, 0.035)));
      plate.current?.setMatrixAt(2 * i, m.compose(p.set(x, -0.034, z), q, s.set(0.09, 0.008, 0.09)));
      plate.current?.setMatrixAt(2 * i + 1, m.compose(p.set(x, floor + 0.004, z), q, s.set(0.1, 0.008, 0.1)));
    });
    for (const r of [stem, plate]) if (r.current) { r.current.instanceMatrix.needsUpdate = true; r.current.computeBoundingSphere(); }
  }, [at, floor]);
  return (
    <group>
      <instancedMesh key={`s${at.length}`} ref={stem} args={[undefined, undefined, at.length]} raycast={() => null}>
        <cylinderGeometry args={[0.5, 0.5, 1, 8]} />
        <meshStandardMaterial color={C.pedestal} metalness={0.1} roughness={0.5} />
      </instancedMesh>
      <instancedMesh key={`p${at.length}`} ref={plate} args={[undefined, undefined, at.length * 2]} raycast={() => null}>
        <boxGeometry args={[1, 1, 1]} />
        <meshStandardMaterial color={C.pedestal} metalness={0.1} roughness={0.5} />
      </instancedMesh>
    </group>
  );
}
