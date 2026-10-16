import { useEffect, useMemo, useRef } from 'react';
import * as THREE from 'three';
import { useFrame, useThree } from '@react-three/fiber';
import type { GBox } from './Scene';

/**
 * The glass studio: the one look the 3D views share, from the compound down
 * to a room. A dark studio with a soft glow behind the subject, floors as
 * frosted-glass trays with a bright rim, glass sides that fade upward, racks
 * in charcoal steel with their doors on and plant a lighter graphite. Physical colours, the
 * same under both themes, like the room's SURF: this is a rendering of the
 * place, not UI chrome.
 *
 * The building rung draws its levels as trays (BuildingScene.tsx); the room
 * rung draws its own floor and walls the same way (Scene.tsx), so descending
 * from the building into a room keeps the picture and adds the detail.
 */
export const GLASS = {
  slab: '#aeb9c7', rim: '#dfe7f0', wall: '#c8d3df', room: '#b9c4d1', roomEdge: '#cfd8e3',
  rack: '#2a2f36', glowIn: '#1d2532', glowOut: '#090c11',
  /* A room's deck: white frosted glass, its tile joints and grid letters a mid grey
     that still reads on white. The building's trays keep the cooler slab above. */
  tile: '#e6ebf0', tileLine: '#88949f', tileInk: '#6f7b89',
  /* Plant is a lighter charcoal than the racks: graphite panels against the racks'
     near-black perforated doors, so one look at a box says which it is, with no colour
     that could read as a measurement. */
  plant: '#4b5560',
};

/** The studio behind the subject: a radial glow from the centre into near-black. */
export function Studio() {
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

/** The renderer's tone curve. The studio is lit like a photograph (filmic),
 *  so its glass and steel read as the building rung tuned them; the plan and
 *  the walk-through keep exact colour, where a white tile is white. Readings
 *  (band colours on the heat map, device faces, aisle tints) opt out of the
 *  curve on their own materials, so a legend band is its hex under either. */
export function Tone({ filmic }: { filmic: boolean }) {
  const { gl, invalidate } = useThree();
  useEffect(() => {
    gl.toneMapping = filmic ? THREE.ACESFilmicToneMapping : THREE.NoToneMapping;
    invalidate();
  }, [gl, filmic, invalidate]);
  return null;
}

/** A box of glass sides over a footprint: brightest at the floor and fading
 *  upward, with a faint rim along the edges. The floor and ceiling are open;
 *  the sides answer no pointer. `cutaway` draws each side from the inside
 *  only, so the walls between the camera and the room fall away as it orbits
 *  and the far walls stay - the room is always seen into, never through its
 *  own front wall. Each wall's rim goes with its pane. */
export function GlassBox({ w, d, cx, cz, y, h, dim = false, cutaway = false }: {
  w: number; d: number; cx: number; cz: number; y: number; h: number; dim?: boolean; cutaway?: boolean;
}) {
  const rim = useMemo(() => new THREE.EdgesGeometry(new THREE.BoxGeometry(w, h, d)), [w, h, d]);
  useEffect(() => () => rim.dispose(), [rim]);
  // One rim per wall: its two uprights and its top and bottom edges, so a culled
  // wall takes its outline with it. Order matches the box faces: +x, -x, +z, -z.
  const wallRims = useMemo(() => {
    const hw = w / 2, hd = d / 2, hh = h / 2;
    const quad = (pts: number[][]) => {
      const g = new THREE.BufferGeometry();
      const seg: number[] = [];
      for (let i = 0; i < 4; i++) seg.push(...pts[i], ...pts[(i + 1) % 4]);
      g.setAttribute('position', new THREE.Float32BufferAttribute(seg, 3));
      return g;
    };
    return [
      quad([[hw, -hh, -hd], [hw, -hh, hd], [hw, hh, hd], [hw, hh, -hd]]),
      quad([[-hw, -hh, -hd], [-hw, -hh, hd], [-hw, hh, hd], [-hw, hh, -hd]]),
      quad([[-hw, -hh, hd], [hw, -hh, hd], [hw, hh, hd], [-hw, hh, hd]]),
      quad([[-hw, -hh, -hd], [hw, -hh, -hd], [hw, hh, -hd], [-hw, hh, -hd]]),
    ];
  }, [w, d, h]);
  useEffect(() => () => wallRims.forEach((g) => g.dispose()), [wallRims]);
  const rimRefs = useRef<(THREE.LineSegments | null)[]>([null, null, null, null]);
  // Which walls face the camera from inside: the camera lies on the inner side of
  // the wall's plane. Those show; the rest are the walls we look through.
  useFrame(({ camera }) => {
    if (!cutaway) return;
    const c = camera.position;
    const inside = [c.x < cx + w / 2, c.x > cx - w / 2, c.z < cz + d / 2, c.z > cz - d / 2];
    rimRefs.current.forEach((l, i) => { if (l) l.visible = inside[i]; });
  });
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
    // Cut away: back faces only, which on a box are the inner faces of the far walls.
    const side = new THREE.MeshBasicMaterial({ color: GLASS.wall, alphaMap: alpha, transparent: true, opacity: dim ? 0.25 : 1,
                                               depthWrite: false, side: cutaway ? THREE.BackSide : THREE.DoubleSide });
    const none = new THREE.MeshBasicMaterial({ visible: false });
    return [side, side, none, none, side, side];
  }, [alpha, dim, cutaway]);
  useEffect(() => () => { mats[0].dispose(); mats[2].dispose(); }, [mats]);
  return (
    <group position={[cx, y + h / 2, cz]}>
      <mesh material={mats} raycast={() => null}>
        <boxGeometry args={[w, h, d]} />
      </mesh>
      {cutaway
        ? wallRims.map((g, i) => (
          <lineSegments key={i} geometry={g} raycast={() => null} ref={(el) => { rimRefs.current[i] = el; }}>
            <lineBasicMaterial color={GLASS.rim} transparent opacity={dim ? 0.12 : 0.45} />
          </lineSegments>
        ))
        : (
          <lineSegments geometry={rim} raycast={() => null}>
            <lineBasicMaterial color={GLASS.rim} transparent opacity={dim ? 0.12 : 0.45} />
          </lineSegments>
        )}
    </group>
  );
}

/** A floor's glass sides: the slab's outline raised to the ceiling. */
export function GlassTray({ geo, y, h, dim }: { geo: THREE.ExtrudeGeometry; y: number; h: number; dim: boolean }) {
  const box = useMemo(() => {
    geo.computeBoundingBox();
    const b = geo.boundingBox as THREE.Box3;
    return { w: b.max.x - b.min.x, d: b.max.y - b.min.y, cx: (b.max.x + b.min.x) / 2, cz: (b.max.y + b.min.y) / 2 };
  }, [geo]);
  return <GlassBox w={box.w} d={box.d} cx={box.cx} cz={box.cz} y={y} h={h} dim={dim} />;
}

/** A rack door seen from the aisle: dark perforated steel in a frame, the
 *  handle, and a few lit status LEDs on the gear behind it. Front and back
 *  both get one - the building does not know which way a rack faces, and a
 *  cabinet has a door at each end anyway. */
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

/** The door skins on a set of plain cabinets, one instanced plane for all. */
export function RackDoors({ boxes }: { boxes: GBox[] }) {
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
