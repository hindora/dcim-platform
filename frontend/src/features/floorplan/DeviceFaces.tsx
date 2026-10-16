import { useEffect, useLayoutEffect, useMemo, useRef } from 'react';
import * as THREE from 'three';
import type { FloorRack, TwinDevice } from '../../api/client';

/**
 * The faceplates of rack-mounted gear, for "See into racks".
 *
 * Real rack gear is a dark steel chassis; what tells a server from a switch
 * across an aisle is its faceplate - drive bays and a vent on a server, rows
 * of RJ45 with link lights and SFP cages on a switch, a few ports and a small
 * display on a router or firewall, power supplies and I/O at the back, a
 * column of outlets on a zero-U PDU. Those are drawn here as textures on the
 * front and rear of each device's box. The colour that carries meaning (the
 * device's category, or its alarm) is on the rack ears at the front, the two
 * strips a real device bolts to the rails by, so no faceplate hides it.
 *
 * Faceplates are generic by type, not per model: the import names a model
 * but carries no front-panel layout.
 */

type Kind = 'server' | 'switch' | 'appliance' | 'generic' | 'pdu';

export function faceKind(t: string): Kind {
  switch (t) {
    case 'server': case 'storage': return 'server';
    case 'switch': case 'oob_switch': return 'switch';
    case 'router': case 'firewall': case 'load_balancer': return 'appliance';
    case 'pdu': case 'floor_pdu': return 'pdu';
    default: return 'generic';
  }
}

const INK = {
  face: '#1c2025', bay: '#0c0f12', bayRim: '#4b535d', port: '#07090b', portRim: '#5a626c',
  vent: '#2b3138', ventHole: '#0d1013', cage: '#9aa3ad', label: '#d9dde2',
  green: '#3ddc84', amber: '#f0a020', blue: '#4ea3ff', cyan: '#2bc4d8',
};

function seeded(seed: number): () => number {
  let s = seed >>> 0;
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
}

const PX_PER_U = 88, WIDTH = 1024;

function canvas(u: number, draw: (c: CanvasRenderingContext2D, w: number, h: number) => void, w = WIDTH): THREE.CanvasTexture {
  const cv = document.createElement('canvas');
  cv.width = w; cv.height = Math.max(32, Math.round(PX_PER_U * u));
  const c = cv.getContext('2d') as CanvasRenderingContext2D;
  draw(c, cv.width, cv.height);
  const t = new THREE.CanvasTexture(cv);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}

function frame(c: CanvasRenderingContext2D, w: number, h: number) {
  c.fillStyle = INK.face; c.fillRect(0, 0, w, h);
  c.strokeStyle = '#0a0c0e'; c.lineWidth = 3; c.strokeRect(1.5, 1.5, w - 3, h - 3);
}
function led(c: CanvasRenderingContext2D, x: number, y: number, r: number, col: string) {
  c.fillStyle = col; c.beginPath(); c.arc(x, y, r, 0, Math.PI * 2); c.fill();
}
function vent(c: CanvasRenderingContext2D, x: number, y: number, w: number, h: number) {
  c.fillStyle = INK.vent; c.fillRect(x, y, w, h);
  c.fillStyle = INK.ventHole;
  const s = Math.max(5, Math.min(9, h / 6));
  for (let yy = y + s / 2; yy < y + h - s / 2; yy += s) {
    for (let xx = x + s / 2 + ((yy / s) % 2) * (s / 2); xx < x + w - s / 2; xx += s) {
      c.beginPath(); c.arc(xx, yy, s * 0.3, 0, Math.PI * 2); c.fill();
    }
  }
}
function port(c: CanvasRenderingContext2D, x: number, y: number, w: number, h: number) {
  c.fillStyle = INK.port; c.fillRect(x, y, w, h);
  c.strokeStyle = INK.portRim; c.lineWidth = 1.5; c.strokeRect(x, y, w, h);
}

function serverFront(u: number) {
  return canvas(u, (c, w, h) => {
    frame(c, w, h);
    const rnd = seeded(7 + u);
    const bayX = w * 0.06, bayW = w * 0.6;
    if (u <= 2) {
      // small-form-factor bays, standing on end
      const n = u === 1 ? 10 : 24, bw = bayW / n;
      for (let i = 0; i < n; i++) {
        const x = bayX + i * bw;
        c.fillStyle = INK.bay; c.fillRect(x + 2, h * 0.12, bw - 4, h * 0.76);
        c.strokeStyle = INK.bayRim; c.lineWidth = 1.5; c.strokeRect(x + 2, h * 0.12, bw - 4, h * 0.76);
        led(c, x + bw / 2, h * 0.8, Math.max(1.5, bw * 0.08), rnd() > 0.15 ? INK.green : INK.amber);
      }
    } else {
      // large-form-factor bays, lying flat, three to a column
      const cols = 4, rows = Math.min(3, u - 1), bw = bayW / cols, bh = (h * 0.84) / rows;
      for (let i = 0; i < cols; i++) for (let j = 0; j < rows; j++) {
        const x = bayX + i * bw, y = h * 0.08 + j * bh;
        c.fillStyle = INK.bay; c.fillRect(x + 3, y + 3, bw - 6, bh - 6);
        c.strokeStyle = INK.bayRim; c.lineWidth = 2; c.strokeRect(x + 3, y + 3, bw - 6, bh - 6);
        led(c, x + bw - 14, y + bh / 2, 3, INK.green);
      }
    }
    vent(c, w * 0.68, h * 0.14, w * 0.2, h * 0.72);
    // control panel: power button, health and ID lights, pull-out label tab
    led(c, w * 0.915, h * 0.35, Math.min(7, h * 0.14), '#3a4048');
    led(c, w * 0.915, h * 0.35, Math.min(4, h * 0.08), INK.green);
    led(c, w * 0.945, h * 0.35, Math.min(3, h * 0.07), INK.blue);
    c.fillStyle = INK.label; c.fillRect(w * 0.9, h * 0.62, w * 0.06, Math.max(3, h * 0.12));
  });
}

function switchFront(u: number) {
  return canvas(u, (c, w, h) => {
    frame(c, w, h);
    const rnd = seeded(31);
    // 48 RJ45 in two rows, blocks of 12, link lights above the top row
    const x0 = w * 0.1, pw = (w * 0.62) / 24, ph = Math.min(h * 0.28, pw * 0.85);
    for (let i = 0; i < 24; i++) {
      const x = x0 + i * pw + Math.floor(i / 6) * 4;
      for (let r = 0; r < 2; r++) port(c, x + 2, h * 0.5 - ph - 2 + r * (ph + 4), pw - 5, ph);
      if (rnd() > 0.3) led(c, x + pw * 0.3, h * 0.5 - ph - 8, 2, INK.green);
      if (rnd() > 0.6) led(c, x + pw * 0.7, h * 0.5 - ph - 8, 2, INK.amber);
    }
    // uplink SFP cages
    for (let i = 0; i < 4; i++) {
      const x = w * 0.78 + i * (w * 0.045), y = h * 0.28, cw = w * 0.036, ch = h * 0.44;
      c.fillStyle = '#05070a'; c.fillRect(x, y, cw, ch);
      c.strokeStyle = INK.cage; c.lineWidth = 2; c.strokeRect(x, y, cw, ch);
      led(c, x + cw / 2, y - 6, 2, i < 2 ? INK.green : '#3a4048');
    }
    // console and management at the left
    port(c, w * 0.035, h * 0.36, w * 0.025, h * 0.28);
    led(c, w * 0.075, h * 0.3, 3, INK.green);
    led(c, w * 0.075, h * 0.55, 3, INK.green);
  });
}

function applianceFront(u: number) {
  return canvas(u, (c, w, h) => {
    frame(c, w, h);
    // the front display
    const lh = Math.min(h * 0.56, 46);
    c.fillStyle = '#04161a'; c.fillRect(w * 0.06, (h - lh) / 2, w * 0.14, lh);
    c.fillStyle = INK.cyan;
    for (let k = 0; k < 2; k++) c.fillRect(w * 0.07, (h - lh) / 2 + 6 + k * (lh / 2.4), w * (k ? 0.07 : 0.1), Math.max(2, lh * 0.12));
    // status lights
    [INK.green, INK.green, INK.amber].forEach((col, i) => led(c, w * 0.24 + i * 14, h * 0.5, 3, col));
    // eight copper ports and two SFP
    const pw = w * 0.04, ph = Math.min(h * 0.4, pw * 0.8);
    for (let i = 0; i < 8; i++) port(c, w * 0.34 + i * (pw + 6), (h - ph) / 2, pw, ph);
    for (let i = 0; i < 2; i++) {
      const x = w * 0.74 + i * (w * 0.05);
      c.fillStyle = '#05070a'; c.fillRect(x, h * 0.3, w * 0.038, h * 0.4);
      c.strokeStyle = INK.cage; c.lineWidth = 2; c.strokeRect(x, h * 0.3, w * 0.038, h * 0.4);
    }
    vent(c, w * 0.86, h * 0.18, w * 0.1, h * 0.64);
  });
}

function genericFront(u: number) {
  return canvas(u, (c, w, h) => {
    frame(c, w, h);
    vent(c, w * 0.06, h * 0.14, w * 0.66, h * 0.72);
    c.fillStyle = '#04161a'; c.fillRect(w * 0.76, h * 0.25, w * 0.12, h * 0.5);
    c.fillStyle = INK.cyan; c.fillRect(w * 0.775, h * 0.38, w * 0.07, Math.max(2, h * 0.1));
    led(c, w * 0.92, h * 0.5, 3, INK.green);
  });
}

function rear(u: number, network: boolean) {
  return canvas(u, (c, w, h) => {
    frame(c, w, h);
    // two hot-swap power supplies at the right: handle, fan, inlet, light
    const pw = w * 0.13, ph = Math.min(h * 0.8, network ? h * 0.8 : 80);
    for (let i = 0; i < 2; i++) {
      const x = w * 0.7 + i * (pw + 8), y = (h - ph) / 2;
      c.fillStyle = '#2a3037'; c.fillRect(x, y, pw, ph);
      c.strokeStyle = '#555e68'; c.lineWidth = 2; c.strokeRect(x, y, pw, ph);
      c.fillStyle = INK.ventHole; c.beginPath(); c.arc(x + pw * 0.35, y + ph / 2, Math.min(ph * 0.36, pw * 0.25), 0, Math.PI * 2); c.fill();
      c.fillStyle = '#05070a'; c.fillRect(x + pw * 0.66, y + ph * 0.3, pw * 0.2, ph * 0.4);
      led(c, x + pw * 0.9, y + ph * 0.2, 2.5, INK.green);
    }
    if (network) {
      // fan trays
      for (let i = 0; i < 2; i++) {
        const x = w * 0.08 + i * w * 0.14, r = Math.min(h * 0.36, w * 0.05);
        c.fillStyle = INK.ventHole; c.beginPath(); c.arc(x + r, h / 2, r, 0, Math.PI * 2); c.fill();
        c.strokeStyle = '#4b535d'; c.lineWidth = 2; c.stroke();
      }
    } else {
      // rear I/O: NIC ports, USB, the management port
      for (let i = 0; i < 4; i++) port(c, w * 0.08 + i * w * 0.045, h * 0.34, w * 0.035, h * 0.32);
      port(c, w * 0.3, h * 0.34, w * 0.035, h * 0.32);
      c.fillStyle = '#1f6feb'; c.fillRect(w * 0.36, h * 0.38, w * 0.02, h * 0.24);
      vent(c, w * 0.42, h * 0.14, w * 0.24, h * 0.72);
    }
  });
}

/** A zero-U PDU's outlet face: an LCD at the top, then C13s in groups. */
function pduFace() {
  const t = canvas(1, (c, w, h) => {
    c.fillStyle = '#16191d'; c.fillRect(0, 0, w, h);
    c.fillStyle = '#04161a'; c.fillRect(w * 0.15, h * 0.02, w * 0.7, h * 0.035);
    c.fillStyle = INK.cyan; c.fillRect(w * 0.22, h * 0.03, w * 0.4, h * 0.012);
    for (let i = 0; i < 24; i++) {
      const y = h * 0.08 + i * (h * 0.9 / 24) + Math.floor(i / 6) * 4;
      c.fillStyle = '#05070a'; c.fillRect(w * 0.22, y, w * 0.56, h * 0.024);
      c.strokeStyle = '#4b535d'; c.lineWidth = 1; c.strokeRect(w * 0.22, y, w * 0.56, h * 0.024);
    }
  }, 64);
  return t;
}


type Box = [number, number, number, number, number, number];

/** Front and rear faceplates and the coloured rack ears for every placed device. */
export function DeviceFaces({ devices, boxes, rackById, colors }: {
  devices: TwinDevice[]; boxes: (Box | null)[]; rackById: Map<string, FloorRack>;
  /** Each device's meaning colour (category or alarm), resolved. */
  colors: THREE.Color[];
}) {
  const textures = useMemo(() => new Map<string, THREE.CanvasTexture>(), []);
  useEffect(() => () => { textures.forEach((t) => t.dispose()); textures.clear(); }, [textures]);
  const tex = (key: string, make: () => THREE.CanvasTexture) => {
    let t = textures.get(key);
    if (!t) { t = make(); textures.set(key, t); }
    return t;
  };

  const { faces, ears } = useMemo(() => {
    const groups = new Map<string, { tex: THREE.CanvasTexture; mats: THREE.Matrix4[] }>();
    const earMats: THREE.Matrix4[] = [], earCols: THREE.Color[] = [];
    const q = new THREE.Quaternion(), up = new THREE.Vector3(0, 1, 0), p = new THREE.Vector3(), s = new THREE.Vector3();
    const put = (key: string, make: () => THREE.CanvasTexture, m: THREE.Matrix4) => {
      const g = groups.get(key);
      if (g) g.mats.push(m); else groups.set(key, { tex: tex(key, make), mats: [m] });
    };
    devices.forEach((d, i) => {
      const b = boxes[i], r = rackById.get(d.rack_id);
      if (!b || !r) return;
      const [x, y, z, w, h, dep] = b;
      const front = r.facing === 'N' ? -1 : 1;
      const kind = faceKind(d.device_type);
      const face = (sign: number, key: string, make: () => THREE.CanvasTexture, fw = w, fh = h) => {
        q.setFromAxisAngle(up, sign > 0 ? 0 : Math.PI);
        put(key, make, new THREE.Matrix4().compose(p.set(x, y, z + sign * (dep / 2 + 0.0015)), q, s.set(fw, fh, 1)));
      };
      if (d.u_start != null && d.u_start > 0) {
        const u = Math.max(1, Math.min(8, Math.round(d.u_height || 1)));
        const make = { server: serverFront, switch: switchFront, appliance: applianceFront, generic: genericFront, pdu: genericFront }[kind];
        face(front, `f:${kind}:${u}`, () => make(u), w - 0.04, h);
        const net = kind === 'switch' || kind === 'appliance';
        face(-front, `r:${net ? 'n' : 's'}:${u}`, () => rear(u, net));
        // the rack ears, in the device's colour
        for (const sx of [-1, 1]) {
          earMats.push(new THREE.Matrix4().compose(p.set(x + sx * (w / 2 - 0.009), y, z + front * (dep / 2 + 0.003)),
            q.identity(), s.set(0.018, h, 0.004)));
          earCols.push(colors[i]);
        }
      } else if (kind === 'pdu' && d.mount === 'zero_u') {
        // a zero-U strip hangs at the rear and shows its outlets to the rear door
        face(-front, 'pdu', pduFace, w * 0.8, h * 0.98);
      }
    });
    return { faces: [...groups.entries()], ears: { mats: earMats, cols: earCols } };
  }, [devices, boxes, rackById, colors]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <group>
      {faces.map(([key, g]) => <FacePlanes key={`${key}${g.mats.length}`} map={g.tex} mats={g.mats} />)}
      <Ears mats={ears.mats} cols={ears.cols} />
    </group>
  );
}

function FacePlanes({ map, mats }: { map: THREE.Texture; mats: THREE.Matrix4[] }) {
  const ref = useRef<THREE.InstancedMesh>(null);
  useLayoutEffect(() => {
    const im = ref.current;
    if (!im) return;
    mats.forEach((m, i) => im.setMatrixAt(i, m));
    im.instanceMatrix.needsUpdate = true;
    im.computeBoundingSphere();
  }, [mats]);
  return (
    <instancedMesh ref={ref} args={[undefined, undefined, mats.length]} frustumCulled={false} raycast={() => null}>
      <planeGeometry args={[1, 1]} />
      <meshBasicMaterial map={map} polygonOffset polygonOffsetFactor={-2} toneMapped={false} />
    </instancedMesh>
  );
}

function Ears({ mats, cols }: { mats: THREE.Matrix4[]; cols: THREE.Color[] }) {
  const ref = useRef<THREE.InstancedMesh>(null);
  useLayoutEffect(() => {
    const im = ref.current;
    if (!im) return;
    mats.forEach((m, i) => { im.setMatrixAt(i, m); im.setColorAt(i, cols[i]); });
    im.instanceMatrix.needsUpdate = true;
    if (im.instanceColor) im.instanceColor.needsUpdate = true;
    im.computeBoundingSphere();
  }, [mats, cols]);
  if (!mats.length) return null;
  return (
    <instancedMesh key={mats.length} ref={ref} args={[undefined, undefined, mats.length]} frustumCulled={false} raycast={() => null}>
      <boxGeometry args={[1, 1, 1]} />
      <meshBasicMaterial toneMapped={false} />
    </instancedMesh>
  );
}
