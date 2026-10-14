import { useEffect, useLayoutEffect, useMemo, useRef } from 'react';
import * as THREE from 'three';
import type { GBox } from './Scene';

/**
 * The hardware a cabinet or a plant unit carries, drawn OUTSIDE its coloured
 * body so a layer's colour is never painted over (the earlier detail layer
 * skinned doors and grilles onto the faces and hid the reading).
 *
 * What stands where, and why it is there in a real room:
 * - **Rack**: four corner rails and a roof lip just proud of the body - the
 *   steel frame of the cabinet - a kick plinth in the 0.1 m base zone under
 *   U1 (no reading lives there), and the swing handle on the front door.
 * - **Floor cabinets** (UPS, RPP, switchgear, MCC, ATS): a kick plinth and one
 *   handle per door section; a UPS shows its controller display.
 * - **Air handler**: a base frame, a handle per service door, the controller
 *   display on the front.
 * - **Chiller / pump**: a concrete housekeeping pad and flanged water nozzles.
 * - **Cooling tower**: open fan stacks on the deck, the colour seen through them.
 * - **Generator**: a pad and the exhaust stack. **Transformer**: a pad,
 *   radiator fin banks off both sides, bushings on the cover.
 *
 * Every part is a physical colour (the same in both themes, like SURF) and none
 * answers the pointer. Units face local -z (the face they discharge from).
 */

const MAT = {
  // Low metalness: the room has no environment map, so a true metal renders black.
  steel: { color: '#3a4048', metalness: 0.1, roughness: 0.6 },
  satin: { color: '#dde1e6', metalness: 0.2, roughness: 0.35 },
  pipe: { color: '#7f8891', metalness: 0.1, roughness: 0.5 },
  galv: { color: '#a3abb3', metalness: 0.15, roughness: 0.55 },
  pad: { color: '#b4b8bd', metalness: 0, roughness: 0.95 },
  fin: { color: '#7c848d', metalness: 0.1, roughness: 0.55 },
  bushing: { color: '#7a4a33', metalness: 0.05, roughness: 0.4 },
} as const;
type Mat = keyof typeof MAT | 'screen';
type Geo = 'box' | 'cyl' | 'tube';

const RAIL = 0.014;        // frame rail section, m
const PLINTH_H = 0.05;     // kick plinth height (inside a rack's base zone)
const PAD_H = 0.04, PAD_OUT = 0.15;

type Part = { geo: Geo; mat: Mat; m: THREE.Matrix4 };

function kindOf(t: string): 'crah' | 'cabinet' | 'ups' | 'chiller' | 'pump' | 'tower' | 'generator' | 'transformer' | 'none' {
  switch (t) {
    case 'crah': case 'crac': case 'in_row_cooler': return 'crah';
    case 'ups': return 'ups';
    case 'rpp': case 'mpp': case 'ats': case 'sts': case 'switchgear': case 'mcc':
    case 'floor_pdu': case 'pdu': case 'battery': case 'cdu': return 'cabinet';
    case 'chiller': return 'chiller';
    case 'pump': return 'pump';
    case 'cooling_tower': case 'dry_cooler': case 'condenser': return 'tower';
    case 'generator': return 'generator';
    case 'transformer': return 'transformer';
    default: return 'none';
  }
}

const UP = new THREE.Vector3(0, 1, 0);
const AXIS = {
  y: new THREE.Quaternion(),
  x: new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 0, 1), Math.PI / 2),
  z: new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), Math.PI / 2),
};

/** Collects parts in a box's local frame (origin at its centre, turned by b.rot). */
function placer(out: Part[], b: GBox) {
  const turn = new THREE.Quaternion().setFromAxisAngle(UP, b.rot);
  const origin = new THREE.Vector3(b.x, b.y, b.z);
  return (geo: Geo, mat: Mat, x: number, y: number, z: number, sx: number, sy: number, sz: number,
          axis: keyof typeof AXIS = 'y') => {
    const p = new THREE.Vector3(x, y, z).applyQuaternion(turn).add(origin);
    const q = turn.clone().multiply(AXIS[axis]);
    out.push({ geo, mat, m: new THREE.Matrix4().compose(p, q, new THREE.Vector3(sx, sy, sz)) });
  };
}

function rackParts(out: Part[], b: GBox, open: boolean) {
  const add = placer(out, b);
  const w = b.w / 2, d = b.d / 2, h = b.h / 2, o = RAIL / 2;
  // corner rails, just outside the body so they frame the colour rather than cover it
  for (const sx of [-1, 1]) for (const sz of [-1, 1]) add('box', 'steel', sx * (w + o), 0, sz * (d + o), RAIL, b.h, RAIL);
  // roof lip, proud of the top edge on all four sides
  for (const sz of [-1, 1]) add('box', 'steel', 0, h - 0.012, sz * (d + o), b.w + 2 * RAIL, 0.024, RAIL);
  for (const sx of [-1, 1]) add('box', 'steel', sx * (w + o), h - 0.012, 0, RAIL, 0.024, b.d);
  // kick plinth: under U1, where the cabinet carries no reading
  add('box', 'steel', 0, -h + PLINTH_H / 2, 0, b.w + 0.012, PLINTH_H, b.d + 0.012);
  // swing handle on the front door, at hand height, on the latch side
  if (b.front && !open) {
    add('box', 'satin', w - 0.07, -h + 1.1, b.front * (d + 0.016), 0.022, 0.2, 0.022);
  }
}

function unitParts(out: Part[], b: GBox, type: string) {
  const kind = kindOf(type);
  if (kind === 'none') return;
  const add = placer(out, b);
  const w = b.w / 2, d = b.d / 2, h = b.h / 2;
  const onFloor = b.y <= h + 0.05;
  const plinth = () => onFloor && add('box', 'steel', 0, -h + PLINTH_H / 2, 0, b.w + 0.016, PLINTH_H, b.d + 0.016);
  const pad = () => onFloor && add('box', 'pad', 0, -h + PAD_H / 2, 0, b.w + 2 * PAD_OUT, PAD_H, b.d + 2 * PAD_OUT);
  const fz = -(d + 0.014);                          // just proud of the front face
  const handle = (x: number, y = -h + Math.min(1.1, b.h * 0.55)) =>
    add('box', 'satin', x, y, fz, 0.02, Math.min(0.22, b.h * 0.12), 0.02);
  const display = (x: number, y: number) => {
    add('box', 'steel', x, y, -(d + 0.008), 0.2, 0.13, 0.016);
    add('box', 'screen', x, y, -(d + 0.017), 0.16, 0.09, 0.004);
  };
  const flanged = (x: number, y: number, z: number, r: number, len: number, axis: 'x' | 'z') => {
    const s = Math.sign(axis === 'x' ? x : z);
    const at = (k: number) => (axis === 'x' ? [x + s * k, y, z] : [x, y, z + s * k]) as [number, number, number];
    add('cyl', 'pipe', ...at(len / 2), r * 2, len, r * 2, axis);
    add('cyl', 'pipe', ...at(len - 0.02), r * 2.6, 0.04, r * 2.6, axis);
  };

  switch (kind) {
    case 'crah': {
      plinth();
      // three service doors; the controller sits on the middle one
      handle(-b.w / 6 - 0.08); handle(b.w / 6 - 0.08); handle(w - 0.08);
      display(0, h - 0.35);
      break;
    }
    case 'cabinet': case 'ups': {
      plinth();
      const n = Math.max(1, Math.round(b.w / 0.8)), sec = b.w / n;
      for (let k = 0; k < n; k++) handle(-w + sec * (k + 1) - 0.07);
      if (kind === 'ups') display(-w + sec / 2, h - 0.3);
      break;
    }
    case 'chiller': {
      pad();
      // evaporator and condenser water boxes on the front end: supply + return each
      const r = Math.min(0.13, b.w * 0.05);
      for (const x of [-b.w * 0.26, b.w * 0.26]) for (const y of [-h + b.h * 0.3, -h + b.h * 0.55]) flanged(x, y, -d, r, 0.32, 'z');
      display(0, h - 0.5);
      break;
    }
    case 'pump': {
      pad();
      const r = Math.min(b.w, b.d) * 0.16;
      const along = b.d >= b.w ? 'z' : 'x';               // in-line: suction one end, discharge the other
      for (const s of [-1, 1]) {
        if (along === 'z') flanged(0, -h + b.h * 0.35, s * d, r, 0.22, 'z');
        else flanged(s * w, -h + b.h * 0.35, 0, r, 0.22, 'x');
      }
      break;
    }
    case 'tower': {
      pad();
      // one fan stack per cell along the long side; open, so the deck's colour reads through
      const alongX = b.w >= b.d, long = Math.max(b.w, b.d), short = Math.min(b.w, b.d);
      const n = Math.max(1, Math.round(long / short)), r = Math.min(short, long / n) * 0.34;
      for (let k = 0; k < n; k++) {
        const t = -long / 2 + (long / n) * (k + 0.5);
        const [x, z] = alongX ? [t, 0] : [0, t];
        add('tube', 'galv', x, h + 0.15, z, r * 2, 0.3, r * 2);
        add('cyl', 'steel', x, h + 0.04, z, r * 0.3, 0.08, r * 0.3);     // fan hub
      }
      break;
    }
    case 'generator': {
      pad();
      // exhaust stack at the radiator (rear) end, off to one side of the roof
      add('cyl', 'pipe', w * 0.55, h + 0.5, d * 0.7, 0.2, 1.0, 0.2);
      add('cyl', 'pipe', w * 0.55, h + 1.0, d * 0.7, 0.26, 0.04, 0.26);
      break;
    }
    case 'transformer': {
      pad();
      // radiator fin banks off both long sides
      const fins = Math.max(4, Math.round((b.d * 0.7) / 0.08));
      for (const sx of [-1, 1]) for (let k = 0; k < fins; k++) {
        const z = -b.d * 0.35 + (b.d * 0.7) * (k + 0.5) / fins;
        add('box', 'fin', sx * (w + 0.16), -h + b.h * 0.45, z, 0.3, b.h * 0.65, 0.012);
      }
      for (const x of [-b.w * 0.25, 0, b.w * 0.25]) add('cyl', 'bushing', x, h + 0.16, -d * 0.4, 0.09, 0.32, 0.09);
      break;
    }
  }
}

/** All cabinet and plant hardware in a handful of instanced draws. */
export function Hardware({ racks, units, unitTypes, open }: {
  racks: GBox[]; units: GBox[]; unitTypes: string[];
  /** "See into racks": the front door is off, so is its handle. */
  open: boolean;
}) {
  const groups = useMemo(() => {
    const parts: Part[] = [];
    for (const b of racks) rackParts(parts, b, open);
    units.forEach((b, i) => unitParts(parts, b, unitTypes[i] ?? ''));
    const g = new Map<string, Part[]>();
    for (const p of parts) {
      const k = `${p.geo}:${p.mat}`;
      const arr = g.get(k);
      if (arr) arr.push(p); else g.set(k, [p]);
    }
    return [...g.entries()];
  }, [racks, units, unitTypes, open]);

  const geos = useMemo(() => ({
    box: new THREE.BoxGeometry(1, 1, 1),
    cyl: new THREE.CylinderGeometry(0.5, 0.5, 1, 20),
    tube: new THREE.CylinderGeometry(0.5, 0.5, 1, 28, 1, true),
  }), []);
  const mats = useMemo(() => {
    const m: Record<string, THREE.Material> = {};
    for (const [k, v] of Object.entries(MAT)) m[k] = new THREE.MeshStandardMaterial(v);
    m.tube = new THREE.MeshStandardMaterial({ ...MAT.galv, side: THREE.DoubleSide });
    m.screen = new THREE.MeshStandardMaterial({ color: '#0d2a33', emissive: '#3cc8e6', emissiveIntensity: 0.85, roughness: 0.3 });
    return m;
  }, []);
  useEffect(() => () => { Object.values(geos).forEach((g) => g.dispose()); }, [geos]);
  useEffect(() => () => { Object.values(mats).forEach((m) => m.dispose()); }, [mats]);

  return (
    <group>
      {groups.map(([k, parts]) => {
        const [geo, mat] = k.split(':') as [Geo, Mat];
        return <PartMesh key={`${k}${parts.length}`} parts={parts} geometry={geos[geo]}
                         material={geo === 'tube' ? mats.tube : mats[mat]} />;
      })}
    </group>
  );
}

function PartMesh({ parts, geometry, material }: { parts: Part[]; geometry: THREE.BufferGeometry; material: THREE.Material }) {
  const ref = useRef<THREE.InstancedMesh>(null);
  useLayoutEffect(() => {
    const im = ref.current;
    if (!im) return;
    parts.forEach((p, i) => im.setMatrixAt(i, p.m));
    im.instanceMatrix.needsUpdate = true;
    im.computeBoundingSphere();
  }, [parts]);
  return <instancedMesh ref={ref} args={[geometry, material, parts.length]} frustumCulled={false} raycast={() => null} />;
}
