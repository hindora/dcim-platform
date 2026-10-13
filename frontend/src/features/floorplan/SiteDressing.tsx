import { useEffect, useMemo } from 'react';
import * as THREE from 'three';
import type { TwinSpace } from '../../api/client';

/**
 * What makes the compound and the building read as a place rather than a
 * diagram, at the site and building rungs - and nothing more than that.
 *
 * The line held throughout: this is a digital twin, not a rendering. Every
 * surface is a drawing surface - a survey grid on the ground, lane lines on
 * the road, bays in the car park, the slab plates and mullions of a curtain
 * wall - and every solid is the simplest body that says what it is: a tank
 * is a cylinder on saddles inside its bund, a guardhouse a box with a window
 * band, the lift core a shaft. No photographs, no trees, no sky. Physical
 * surfaces keep their own colours under both themes, the way the room's do;
 * only the lines follow the chrome.
 *
 * Nothing here answers the pointer. The zone surfaces replace the flat grey
 * polygons and keep their tooltips; everything else is inert.
 */

const SURF = {
  ground: '#c9cdd3', gridInk: 'rgba(30,34,40,0.10)',
  asphalt: '#4b5057', asphaltEdge: 'rgba(240,242,245,0.85)', lane: 'rgba(240,242,245,0.6)',
  concrete: '#a7adb5', joint: 'rgba(30,34,40,0.22)', bay: 'rgba(240,242,245,0.7)',
  steel: '#9aa1ab', fencePost: '#6f7680', barrierRed: '#c62a26', barrierWhite: '#f1f2f4',
  tank: '#dde1e6', tankBand: '#8c939c', bund: '#8e949c', house: '#b7bcc4', houseEdge: '#4b535d',
  glass: '#8fb8d3', spandrel: '#5d6672', plinth: '#3a4048', parapet: '#6b737e', column: '#7c8591',
  canopy: '#4b535d', shadow: 'rgba(0,0,0,1)',
};
const FENCE_H = 2.4;
const PX_PER_M = 14;

function canvasTex(w: number, h: number, draw: (c: CanvasRenderingContext2D, w: number, h: number) => void): THREE.CanvasTexture {
  const cv = document.createElement('canvas');
  cv.width = Math.max(2, Math.round(w)); cv.height = Math.max(2, Math.round(h));
  const c = cv.getContext('2d');
  if (c) draw(c, cv.width, cv.height);
  const t = new THREE.CanvasTexture(cv);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 8;
  return t;
}

/** A flat textured rectangle on the ground: plan x east, plan y south; the
 *  canvas's top row is north. */
function Sheet({ x, z, w, d, y, tex, opacity = 1, order }: {
  x: number; z: number; w: number; d: number; y: number; tex: THREE.Texture; opacity?: number; order?: number;
}) {
  return (
    <mesh position={[x, y, z]} rotation-x={-Math.PI / 2} raycast={() => null} renderOrder={order}>
      <planeGeometry args={[w, d]} />
      <meshBasicMaterial map={tex} transparent={opacity < 1 || tex.format === THREE.RGBAFormat} opacity={opacity}
                         depthWrite={false} polygonOffset polygonOffsetFactor={-1} />
    </mesh>
  );
}

// --------------------------------------------------------------- ground

/** The compound's ground with a one-metre survey grid, and the land around
 *  it as a fainter five-metre grid on nothing - the twin's edge, not a
 *  horizon. */
export function Ground({ ground, X, Z, faint }: {
  ground: { x0: number; y0: number; x1: number; y1: number };
  X: (x: number) => number; Z: (y: number) => number;
  /** The chrome's faint line colour, for the outer grid. */
  faint: string;
}) {
  const w = ground.x1 - ground.x0, d = ground.y1 - ground.y0;
  const inner = useMemo(() => canvasTex(w * PX_PER_M, d * PX_PER_M, (c, cw, ch) => {
    c.fillStyle = SURF.ground; c.fillRect(0, 0, cw, ch);
    c.strokeStyle = SURF.gridInk; c.lineWidth = 1;
    for (let m = 0; m <= w; m++) { const x = m * PX_PER_M + 0.5; c.beginPath(); c.moveTo(x, 0); c.lineTo(x, ch); c.stroke(); }
    for (let m = 0; m <= d; m++) { const y = m * PX_PER_M + 0.5; c.beginPath(); c.moveTo(0, y); c.lineTo(cw, y); c.stroke(); }
    c.strokeStyle = 'rgba(30,34,40,0.2)'; c.lineWidth = 2;
    for (let m = 0; m <= w; m += 5) { const x = m * PX_PER_M; c.beginPath(); c.moveTo(x, 0); c.lineTo(x, ch); c.stroke(); }
    for (let m = 0; m <= d; m += 5) { const y = m * PX_PER_M; c.beginPath(); c.moveTo(0, y); c.lineTo(cw, y); c.stroke(); }
  }), [w, d]);
  const margin = Math.max(w, d) * 0.5;
  const ow = w + 2 * margin, od = d + 2 * margin;
  const outer = useMemo(() => canvasTex(ow * 4, od * 4, (c, cw, ch) => {
    c.clearRect(0, 0, cw, ch);
    c.strokeStyle = faint; c.lineWidth = 1;
    for (let m = 0; m <= ow; m += 5) { const x = m * 4 + 0.5; c.beginPath(); c.moveTo(x, 0); c.lineTo(x, ch); c.stroke(); }
    for (let m = 0; m <= od; m += 5) { const y = m * 4 + 0.5; c.beginPath(); c.moveTo(0, y); c.lineTo(cw, y); c.stroke(); }
  }), [ow, od, faint]);
  useEffect(() => () => { inner.dispose(); outer.dispose(); }, [inner, outer]);
  const cx = X((ground.x0 + ground.x1) / 2), cz = Z((ground.y0 + ground.y1) / 2);
  return (
    <group>
      <Sheet x={cx} z={cz} w={ow} d={od} y={-0.06} tex={outer} opacity={0.35} />
      <Sheet x={cx} z={cz} w={w} d={d} y={-0.03} tex={inner} />
    </group>
  );
}

// ---------------------------------------------------------------- zones

type BB = { x0: number; y0: number; x1: number; y1: number };

/** A road, a driveway, an apron or a car park: the surface with its
 *  markings. Returns null for a zone kind that has no surface of its own. */
export function ZoneSurface({ z, bb, X, Z }: { z: TwinSpace; bb: BB; X: (x: number) => number; Z: (y: number) => number }) {
  const w = bb.x1 - bb.x0, d = bb.y1 - bb.y0;
  const alongX = w >= d;
  const kind = z.kind === 'road' || z.kind === 'driveway' || z.kind === 'apron' || z.kind === 'parking' ? z.kind : null;
  const tex = useMemo(() => {
    if (!kind) return null;
    return canvasTex(w * PX_PER_M, d * PX_PER_M, (c, cw, ch) => {
      const m = PX_PER_M;
      if (kind === 'road' || kind === 'driveway') {
        c.fillStyle = SURF.asphalt; c.fillRect(0, 0, cw, ch);
        c.strokeStyle = SURF.asphaltEdge; c.lineWidth = 0.12 * m;
        if (alongX) { for (const y of [0.2 * m, ch - 0.2 * m]) { c.beginPath(); c.moveTo(0, y); c.lineTo(cw, y); c.stroke(); } }
        else { for (const x of [0.2 * m, cw - 0.2 * m]) { c.beginPath(); c.moveTo(x, 0); c.lineTo(x, ch); c.stroke(); } }
        // A driveway is one lane: edges only, no centre line.
        if (kind === 'driveway') return;
        c.strokeStyle = SURF.lane; c.setLineDash([1.5 * m, 1.0 * m]);
        c.beginPath();
        if (alongX) { c.moveTo(0, ch / 2); c.lineTo(cw, ch / 2); } else { c.moveTo(cw / 2, 0); c.lineTo(cw / 2, ch); }
        c.stroke(); c.setLineDash([]);
      } else if (kind === 'apron') {
        c.fillStyle = SURF.concrete; c.fillRect(0, 0, cw, ch);
        c.strokeStyle = SURF.joint; c.lineWidth = 2;
        for (let k = 0; k <= w; k += 5) { const x = k * m + 0.5; c.beginPath(); c.moveTo(x, 0); c.lineTo(x, ch); c.stroke(); }
        for (let k = 0; k <= d; k += 5) { const y = k * m + 0.5; c.beginPath(); c.moveTo(0, y); c.lineTo(cw, y); c.stroke(); }
        // the hatched strip at the building's edge, where the dock is
        c.strokeStyle = 'rgba(228,179,32,0.75)'; c.lineWidth = 0.1 * m;
        c.beginPath(); c.moveTo(0, 0.3 * m); c.lineTo(cw, 0.3 * m); c.stroke();
      } else {
        c.fillStyle = SURF.asphalt; c.fillRect(0, 0, cw, ch);
        c.strokeStyle = SURF.bay; c.lineWidth = 0.1 * m;
        const pitch = 2.5 * m;
        if (alongX) {
          for (let x = 0; x <= cw + 1; x += pitch) { c.beginPath(); c.moveTo(x, 0.2 * m); c.lineTo(x, ch - 0.2 * m); c.stroke(); }
          c.beginPath(); c.moveTo(0, ch - 0.2 * m); c.lineTo(cw, ch - 0.2 * m); c.stroke();
        } else {
          for (let y = 0; y <= ch + 1; y += pitch) { c.beginPath(); c.moveTo(0.2 * m, y); c.lineTo(cw - 0.2 * m, y); c.stroke(); }
          c.beginPath(); c.moveTo(cw - 0.2 * m, 0); c.lineTo(cw - 0.2 * m, ch); c.stroke();
        }
      }
    });
  }, [kind, w, d, alongX]);
  useEffect(() => () => tex?.dispose(), [tex]);
  if (!tex) return null;
  return <Sheet x={X((bb.x0 + bb.x1) / 2)} z={Z((bb.y0 + bb.y1) / 2)} w={w} d={d} y={0.012} tex={tex} />;
}

/** The solids a compound zone stands for: a tank, a small building, the
 *  ducts of a telecom entrance, the barrier at the gate. */
export function ZoneObject({ z, bb, X, Z, ink }: { z: TwinSpace; bb: BB; X: (x: number) => number; Z: (y: number) => number; ink: string }) {
  const w = bb.x1 - bb.x0, d = bb.y1 - bb.y0;
  const cx = X((bb.x0 + bb.x1) / 2), cz = Z((bb.y0 + bb.y1) / 2);
  switch (z.kind) {
    case 'fuel_tank': return <FuelTank cx={cx} cz={cz} w={w} d={d} />;
    case 'water_tank': return <WaterTank cx={cx} cz={cz} w={w} d={d} />;
    case 'guardhouse': return <SmallHouse cx={cx} cz={cz} w={w} d={d} h={3.0} band="glass" />;
    case 'fire_pump_house': return <SmallHouse cx={cx} cz={cz} w={w} d={d} h={3.6} band="fire" />;
    case 'telecom_entrance': return <Ducts bb={bb} X={X} Z={Z} ink={ink} />;
    default: return null;
  }
}

function FuelTank({ cx, cz, w, d }: { cx: number; cz: number; w: number; d: number }) {
  const alongX = w >= d;
  const len = Math.max(w, d) * 0.8, r = Math.min(Math.min(w, d) * 0.36, 1.4);
  const y = 0.5 + r;
  return (
    <group position={[cx, 0, cz]} raycast={() => null}>
      {/* the bund: a low wall round the zone that holds the tank's contents if it fails */}
      {[[0, -d / 2, w, 0.15], [0, d / 2, w, 0.15], [-w / 2, 0, 0.15, d], [w / 2, 0, 0.15, d]].map(([x, z, bw, bd], i) => (
        <mesh key={i} position={[x, 0.3, z]}>
          <boxGeometry args={[bw, 0.6, bd]} /><meshStandardMaterial color={SURF.bund} roughness={1} />
        </mesh>
      ))}
      {[-0.3, 0.3].map((k) => (
        <mesh key={k} position={alongX ? [k * len, 0.25, 0] : [0, 0.25, k * len]}>
          <boxGeometry args={alongX ? [0.4, 0.5, r * 1.6] : [r * 1.6, 0.5, 0.4]} /><meshStandardMaterial color={SURF.bund} roughness={1} />
        </mesh>
      ))}
      <mesh position={[0, y, 0]} rotation-z={alongX ? Math.PI / 2 : 0} rotation-x={alongX ? 0 : Math.PI / 2}>
        <cylinderGeometry args={[r, r, len, 24]} /><meshStandardMaterial color={SURF.tank} roughness={0.45} />
      </mesh>
      {[-1, 1].map((k) => (
        <mesh key={k} position={alongX ? [k * len / 2, y, 0] : [0, y, k * len / 2]} rotation-z={alongX ? Math.PI / 2 : 0} rotation-x={alongX ? 0 : Math.PI / 2}>
          <sphereGeometry args={[r, 24, 12, 0, Math.PI * 2, 0, Math.PI / 2]} /><meshStandardMaterial color={SURF.tank} roughness={0.45} />
        </mesh>
      ))}
      {/* the manway and the fill point on top */}
      <mesh position={[0, y + r, 0]}>
        <cylinderGeometry args={[0.3, 0.3, 0.3, 16]} /><meshStandardMaterial color={SURF.tankBand} roughness={0.6} />
      </mesh>
      <ContactShadow x={0} z={0} w={len * 1.2} d={r * 3.2} />
    </group>
  );
}

function WaterTank({ cx, cz, w, d }: { cx: number; cz: number; w: number; d: number }) {
  const r = Math.min(w, d) * 0.42, h = Math.min(6, Math.max(3, r * 3));
  return (
    <group position={[cx, 0, cz]} raycast={() => null}>
      <mesh position={[0, h / 2, 0]}>
        <cylinderGeometry args={[r, r, h, 28]} /><meshStandardMaterial color={SURF.tank} roughness={0.5} />
      </mesh>
      {[0.25, 0.5, 0.75].map((k) => (
        <mesh key={k} position={[0, h * k, 0]}>
          <torusGeometry args={[r + 0.02, 0.03, 6, 40]} /><meshStandardMaterial color={SURF.tankBand} roughness={0.6} />
        </mesh>
      ))}
      <mesh position={[0, h + 0.1, 0]}>
        <cylinderGeometry args={[r * 1.02, r * 1.02, 0.2, 28]} /><meshStandardMaterial color={SURF.tankBand} roughness={0.6} />
      </mesh>
      <ContactShadow x={0} z={0} w={r * 3} d={r * 3} />
    </group>
  );
}

function SmallHouse({ cx, cz, w, d, h, band }: { cx: number; cz: number; w: number; d: number; h: number; band: 'glass' | 'fire' }) {
  const geo = useMemo(() => new THREE.BoxGeometry(w * 0.9, h, d * 0.9), [w, h, d]);
  useEffect(() => () => geo.dispose(), [geo]);
  return (
    <group position={[cx, 0, cz]} raycast={() => null}>
      <mesh geometry={geo} position={[0, h / 2, 0]}>
        <meshStandardMaterial color={SURF.house} roughness={0.9} />
      </mesh>
      <lineSegments position={[0, h / 2, 0]}>
        <edgesGeometry args={[geo]} /><lineBasicMaterial color={SURF.houseEdge} transparent opacity={0.7} />
      </lineSegments>
      {/* a window band on a guardhouse, the red band of a fire pump house */}
      <mesh position={[0, band === 'glass' ? h * 0.55 : h * 0.3, 0]}>
        <boxGeometry args={[w * 0.9 + 0.02, band === 'glass' ? 0.7 : 0.25, d * 0.9 + 0.02]} />
        <meshStandardMaterial color={band === 'glass' ? SURF.glass : SURF.barrierRed} roughness={0.3}
                              transparent={band === 'glass'} opacity={band === 'glass' ? 0.8 : 1} />
      </mesh>
      <mesh position={[0, h + 0.08, 0]}>
        <boxGeometry args={[w * 0.9 + 0.2, 0.16, d * 0.9 + 0.2]} /><meshStandardMaterial color={SURF.plinth} roughness={1} />
      </mesh>
      <ContactShadow x={0} z={0} w={w * 1.3} d={d * 1.3} />
    </group>
  );
}

/** Two diverse duct routes, dashed on the ground. */
function Ducts({ bb, X, Z, ink }: { bb: BB; X: (x: number) => number; Z: (y: number) => number; ink: string }) {
  const geo = useMemo(() => {
    const alongX = bb.x1 - bb.x0 >= bb.y1 - bb.y0;
    const cy = (bb.y0 + bb.y1) / 2, cxm = (bb.x0 + bb.x1) / 2;
    const pts: THREE.Vector3[] = [];
    for (const off of [-0.2, 0.2]) {
      if (alongX) pts.push(new THREE.Vector3(X(bb.x0), 0.02, Z(cy + off)), new THREE.Vector3(X(bb.x1), 0.02, Z(cy + off)));
      else pts.push(new THREE.Vector3(X(cxm + off), 0.02, Z(bb.y0)), new THREE.Vector3(X(cxm + off), 0.02, Z(bb.y1)));
    }
    const g = new THREE.BufferGeometry().setFromPoints(pts);
    return g;
  }, [bb, X, Z]);
  useEffect(() => () => geo.dispose(), [geo]);
  // Dashes need per-vertex distances; lineSegments pairs the points.
  const seg = useMemo(() => { const l = new THREE.LineSegments(geo, new THREE.LineDashedMaterial({ color: ink, dashSize: 0.8, gapSize: 0.5, transparent: true, opacity: 0.8 })); l.computeLineDistances(); return l; }, [geo, ink]);
  useEffect(() => () => (seg.material as THREE.Material).dispose(), [seg]);
  return <primitive object={seg} raycast={() => null} />;
}

// ---------------------------------------------------------------- fence

/** Posts, three rails and a faint mesh round the compound, with the gate
 *  left open and a barrier across it. */
export function Fence({ poly, gate, X, Z, ink }: {
  poly: number[][]; gate: BB | null; X: (x: number) => number; Z: (y: number) => number; ink: string;
}) {
  const built = useMemo(() => {
    const posts: [number, number][] = [];
    const rails: number[] = [];
    const panels: { x: number; z: number; len: number; ry: number }[] = [];
    const inGate = (x: number, y: number) => gate != null && x >= gate.x0 - 0.3 && x <= gate.x1 + 0.3 && y >= gate.y0 - 0.3 && y <= gate.y1 + 0.3;
    const n = poly.length;
    for (let i = 0; i < n; i++) {
      const [ax, ay] = poly[i], [bx, by] = poly[(i + 1) % n];
      const len = Math.hypot(bx - ax, by - ay);
      const steps = Math.max(1, Math.round(len / 2.5));
      let runStart: [number, number] | null = null;
      for (let k = 0; k <= steps; k++) {
        const x = ax + ((bx - ax) * k) / steps, y = ay + ((by - ay) * k) / steps;
        if (!inGate(x, y)) {
          posts.push([x, y]);
          if (runStart == null) runStart = [x, y];
        }
        const endRun = inGate(x, y) || k === steps;
        if (endRun && runStart != null) {
          const ex = inGate(x, y) ? ax + ((bx - ax) * (k - 1)) / steps : x, ey = inGate(x, y) ? ay + ((by - ay) * (k - 1)) / steps : y;
          for (const h of [0.1, FENCE_H * 0.5, FENCE_H]) rails.push(X(runStart[0]), h, Z(runStart[1]), X(ex), h, Z(ey));
          const l = Math.hypot(ex - runStart[0], ey - runStart[1]);
          if (l > 0.5) panels.push({ x: X((runStart[0] + ex) / 2), z: Z((runStart[1] + ey) / 2), len: l, ry: -Math.atan2(ey - runStart[1], ex - runStart[0]) });
          runStart = null;
        }
      }
    }
    const railGeo = new THREE.BufferGeometry();
    railGeo.setAttribute('position', new THREE.Float32BufferAttribute(rails, 3));
    return { posts, railGeo, panels };
  }, [poly, gate, X, Z]);
  useEffect(() => () => built.railGeo.dispose(), [built]);
  const postGeo = useMemo(() => new THREE.BoxGeometry(0.1, FENCE_H, 0.1), []);
  useEffect(() => () => postGeo.dispose(), [postGeo]);
  return (
    <group raycast={() => null}>
      {built.posts.map(([x, y], i) => (
        <mesh key={i} geometry={postGeo} position={[X(x), FENCE_H / 2, Z(y)]}>
          <meshStandardMaterial color={SURF.fencePost} roughness={0.8} />
        </mesh>
      ))}
      <lineSegments geometry={built.railGeo}><lineBasicMaterial color={ink} transparent opacity={0.7} /></lineSegments>
      {built.panels.map((pn, i) => (
        <mesh key={i} position={[pn.x, FENCE_H / 2, pn.z]} rotation-y={pn.ry}>
          <planeGeometry args={[pn.len, FENCE_H]} />
          <meshBasicMaterial color={SURF.steel} transparent opacity={0.08} side={THREE.DoubleSide} depthWrite={false} />
        </mesh>
      ))}
      {gate && <Barrier gate={gate} X={X} Z={Z} />}
    </group>
  );
}

function Barrier({ gate, X, Z }: { gate: BB; X: (x: number) => number; Z: (y: number) => number }) {
  const w = gate.x1 - gate.x0, d = gate.y1 - gate.y0;
  const alongX = w >= d;
  const len = Math.max(w, d);
  const tex = useMemo(() => canvasTex(256, 16, (c, cw, ch) => {
    for (let i = 0; i < 8; i++) { c.fillStyle = i % 2 ? SURF.barrierWhite : SURF.barrierRed; c.fillRect((cw / 8) * i, 0, cw / 8, ch); }
  }), []);
  useEffect(() => () => tex.dispose(), [tex]);
  const px = alongX ? X(gate.x0) : X((gate.x0 + gate.x1) / 2), pz = alongX ? Z((gate.y0 + gate.y1) / 2) : Z(gate.y0);
  return (
    <group position={[px, 0, pz]} rotation-y={alongX ? 0 : -Math.PI / 2}>
      <mesh position={[0, 0.5, 0]}>
        <boxGeometry args={[0.35, 1.0, 0.35]} /><meshStandardMaterial color={SURF.plinth} roughness={0.8} />
      </mesh>
      <mesh position={[len / 2, 0.95, 0]}>
        <boxGeometry args={[len, 0.1, 0.1]} /><meshStandardMaterial map={tex} roughness={0.6} />
      </mesh>
    </group>
  );
}

// -------------------------------------------------------------- building

/** The glazed shell seen from the compound: slab plates and spandrel bands
 *  behind the glass, mullions on the glass, a plinth, a parapet, and a
 *  canopy at the entrance and the dock. The click target stays the
 *  caller's shell box; this is drawn around it and takes no pointer. */
export function Facade({ box, top, levels, openings, hot, ink, accent }: {
  box: BB; top: number; levels: { name: string; elevation_m: number }[];
  /** Entrance and dock openings on the building's edge, from the fabric. */
  openings: { bb: BB; kind: 'entrance' | 'dock' }[];
  hot: boolean; ink: string; accent: string;
}) {
  const w = box.x1 - box.x0, d = box.y1 - box.y0;
  const cx = (box.x0 + box.x1) / 2, cz = (box.y0 + box.y1) / 2;
  // In the scene frame the building's box is the origin, so local = plan - centre.
  const lx = (x: number) => x - cx, lz = (y: number) => y - cz;
  const mullions = useMemo(() => {
    const pts: number[] = [];
    const pitch = 1.5;
    const faces: [number, number, number, number][] = [
      [lx(box.x0), lz(box.y0), lx(box.x1), lz(box.y0)], [lx(box.x1), lz(box.y0), lx(box.x1), lz(box.y1)],
      [lx(box.x1), lz(box.y1), lx(box.x0), lz(box.y1)], [lx(box.x0), lz(box.y1), lx(box.x0), lz(box.y0)],
    ];
    for (const [ax, az, bx, bz] of faces) {
      const len = Math.hypot(bx - ax, bz - az), n = Math.max(1, Math.round(len / pitch));
      for (let k = 1; k < n; k++) {
        const x = ax + ((bx - ax) * k) / n, z = az + ((bz - az) * k) / n;
        pts.push(x, 0.6, z, x, top, z);
      }
      for (const l of levels) {
        if (l.elevation_m <= 0 || l.elevation_m >= top) continue;
        pts.push(ax, l.elevation_m + 1.2, az, bx, l.elevation_m + 1.2, bz);   // the transom over each spandrel
      }
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
    return g;
  }, [box, top, levels]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => mullions.dispose(), [mullions]);
  const ring = (y: number, h: number, t: number, colour: string, opacity = 1) => (
    <group position={[0, y, 0]}>
      {[[0, lz(box.y0), w + 2 * t, t], [0, lz(box.y1), w + 2 * t, t], [lx(box.x0), 0, t, d], [lx(box.x1), 0, t, d]].map(([x, z, bw, bd], i) => (
        <mesh key={i} position={[x, 0, z]}>
          <boxGeometry args={[bw, h, bd]} />
          <meshStandardMaterial color={colour} roughness={0.9} transparent={opacity < 1} opacity={opacity} />
        </mesh>
      ))}
    </group>
  );
  return (
    <group raycast={() => null}>
      {/* the glass, a hair outside the click box so the shell's edge lines stay crisp */}
      <mesh position={[0, top / 2, 0]}>
        <boxGeometry args={[w + 0.04, top, d + 0.04]} />
        <meshStandardMaterial color={SURF.glass} transparent opacity={hot ? 0.22 : 0.14} roughness={0.15} metalness={0.1}
                              depthWrite={false} side={THREE.FrontSide} />
      </mesh>
      <lineSegments geometry={mullions}>
        <lineBasicMaterial color={hot ? accent : ink} transparent opacity={hot ? 0.5 : 0.28} />
      </lineSegments>
      {/* spandrel bands: the slab edge and the upstand over it, which is what reads as "floors" through glass */}
      {levels.filter((l) => l.elevation_m > 0 && l.elevation_m < top).map((l) => (
        <group key={l.name}>{ring(l.elevation_m + 0.3, 0.9, 0.06, SURF.spandrel, 0.95)}</group>
      ))}
      {ring(0.3, 0.6, 0.12, SURF.plinth)}
      {ring(top + 0.5, 1.0, 0.12, SURF.parapet, 0.95)}
      {openings.map((o, i) => <Canopy key={i} o={o} box={box} lx={lx} lz={lz} />)}
      <ContactShadow x={0} z={0} w={w * 1.3} d={d * 1.3} strength={0.5} />
    </group>
  );
}

/** A canopy over an entrance or a dock door, projecting out from the wall
 *  the opening is in. */
function Canopy({ o, box, lx, lz }: { o: { bb: BB; kind: 'entrance' | 'dock' }; box: BB; lx: (x: number) => number; lz: (y: number) => number }) {
  const ox = (o.bb.x0 + o.bb.x1) / 2, oy = (o.bb.y0 + o.bb.y1) / 2;
  const gaps: ['N' | 'S' | 'W' | 'E', number][] = [['N', Math.abs(oy - box.y0)], ['S', Math.abs(oy - box.y1)], ['W', Math.abs(ox - box.x0)], ['E', Math.abs(ox - box.x1)]];
  const wall = gaps.sort((a, b) => a[1] - b[1])[0][0];
  const span = Math.max(o.kind === 'dock' ? 5 : 3.6, (wall === 'N' || wall === 'S' ? o.bb.x1 - o.bb.x0 : o.bb.y1 - o.bb.y0) + 1.2);
  const out = o.kind === 'dock' ? 3.0 : 2.0, h = o.kind === 'dock' ? 4.2 : 3.0;
  const along = wall === 'N' || wall === 'S';
  const dir = wall === 'N' ? -1 : wall === 'S' ? 1 : wall === 'W' ? -1 : 1;
  const x = along ? lx(ox) : lx(wall === 'W' ? box.x0 : box.x1) + dir * out / 2;
  const z = along ? lz(wall === 'N' ? box.y0 : box.y1) + dir * out / 2 : lz(oy);
  return (
    <group raycast={() => null}>
      <mesh position={[x, h, z]}>
        <boxGeometry args={along ? [span, 0.18, out] : [out, 0.18, span]} />
        <meshStandardMaterial color={SURF.canopy} roughness={0.8} />
      </mesh>
      {[-1, 1].map((k) => (
        <mesh key={k} position={along ? [x + k * (span / 2 - 0.25), h / 2, z + dir * (out / 2 - 0.2)] : [x + dir * (out / 2 - 0.2), h / 2, z + k * (span / 2 - 0.25)]}>
          <boxGeometry args={[0.12, h, 0.12]} /><meshStandardMaterial color={SURF.canopy} roughness={0.8} />
        </mesh>
      ))}
      {o.kind === 'dock' && (
        // the dock's raised platform and its bumpers
        <mesh position={along ? [x, 0.6, z] : [x, 0.6, z]}>
          <boxGeometry args={along ? [span, 1.2, out * 0.5] : [out * 0.5, 1.2, span]} />
          <meshStandardMaterial color={SURF.concrete} roughness={1} />
        </mesh>
      )}
    </group>
  );
}

/** The structural grid, seen at the building rung: columns on the perimeter
 *  at the bay pitch, ground to roof. */
export function Columns({ box, top, dim }: { box: BB; top: number; dim: boolean }) {
  const pts = useMemo(() => {
    const w = box.x1 - box.x0, d = box.y1 - box.y0;
    const nx = Math.max(1, Math.round(w / 6.5)), nz = Math.max(1, Math.round(d / 6.5));
    const cx = (box.x0 + box.x1) / 2, cz = (box.y0 + box.y1) / 2;
    const out: [number, number][] = [];
    for (let i = 0; i <= nx; i++) for (const y of [box.y0 + 0.25, box.y1 - 0.25]) out.push([box.x0 + (w * i) / nx - cx + (i === 0 ? 0.25 : i === nx ? -0.25 : 0), y - cz]);
    for (let j = 1; j < nz; j++) for (const x of [box.x0 + 0.25, box.x1 - 0.25]) out.push([x - cx, box.y0 + (d * j) / nz - cz]);
    return out;
  }, [box]);
  const geo = useMemo(() => new THREE.BoxGeometry(0.4, top, 0.4), [top]);
  useEffect(() => () => geo.dispose(), [geo]);
  return (
    <group raycast={() => null}>
      {pts.map(([x, z], i) => (
        <mesh key={i} geometry={geo} position={[x, top / 2, z]}>
          <meshStandardMaterial color={SURF.column} roughness={0.9} transparent opacity={dim ? 0.12 : 0.7} />
        </mesh>
      ))}
    </group>
  );
}

/** The stair and lift core as a shaft through every level, with the lift's
 *  overrun above the roof. */
export function CoreShaft({ poly, top, X, Z, ink, quiet }: {
  poly: number[][]; top: number; X: (x: number) => number; Z: (y: number) => number; ink: string; quiet: boolean;
}) {
  const bb = useMemo(() => ({ x0: Math.min(...poly.map((p) => p[0])), y0: Math.min(...poly.map((p) => p[1])),
                               x1: Math.max(...poly.map((p) => p[0])), y1: Math.max(...poly.map((p) => p[1])) }), [poly]);
  const h = top + 1.2;
  const geo = useMemo(() => new THREE.BoxGeometry((bb.x1 - bb.x0) * 0.96, h, (bb.y1 - bb.y0) * 0.96), [bb, h]);
  useEffect(() => () => geo.dispose(), [geo]);
  return (
    <group position={[X((bb.x0 + bb.x1) / 2), h / 2, Z((bb.y0 + bb.y1) / 2)]} raycast={() => null}>
      <mesh geometry={geo}>
        <meshStandardMaterial color={SURF.spandrel} roughness={1} transparent opacity={quiet ? 0.18 : 0.3} depthWrite={false} />
      </mesh>
      <lineSegments>
        <edgesGeometry args={[geo]} /><lineBasicMaterial color={ink} transparent opacity={quiet ? 0.35 : 0.6} />
      </lineSegments>
    </group>
  );
}

/** A soft dark pool under a body: the one shading cue a flat-lit twin
 *  needs to sit its solids on the ground. */
export function ContactShadow({ x, z, w, d, strength = 0.4 }: { x: number; z: number; w: number; d: number; strength?: number }) {
  const tex = useMemo(() => canvasTex(128, 128, (c, cw, ch) => {
    const g = c.createRadialGradient(cw / 2, ch / 2, 0, cw / 2, ch / 2, cw / 2);
    g.addColorStop(0, 'rgba(0,0,0,0.9)'); g.addColorStop(0.55, 'rgba(0,0,0,0.45)'); g.addColorStop(1, 'rgba(0,0,0,0)');
    c.fillStyle = g; c.fillRect(0, 0, cw, ch);
  }), []);
  useEffect(() => () => tex.dispose(), [tex]);
  return (
    <mesh position={[x, 0.006, z]} rotation-x={-Math.PI / 2} raycast={() => null} renderOrder={1}>
      <planeGeometry args={[w, d]} />
      <meshBasicMaterial map={tex} transparent opacity={strength} depthWrite={false} />
    </mesh>
  );
}
