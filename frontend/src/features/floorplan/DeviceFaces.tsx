import { useEffect, useLayoutEffect, useMemo, useRef } from 'react';
import * as THREE from 'three';
import type { FloorRack, TwinDevice } from '../../api/client';
import { RACK_BASE, U } from './layers';

/**
 * The faceplates of rack-mounted gear, for "See into racks" and the rack's
 * own view.
 *
 * Real rack gear is a dark steel chassis; what tells one box from another
 * across an aisle is its front: the drive carriers and their latches, the
 * control panel, the port cages, and the maker's badge. A PowerEdge is black
 * with a blue ID button and a row of SFF carriers; a ProLiant is graphite
 * with the green badge and grey Smart Carriers; a ThinkSystem has orange-red
 * latch tabs; a Supermicro has perforated carriers with green hot-swap tabs;
 * a Nexus is two rows of SFP28 cages with QSFP uplinks on the right, a
 * Catalyst four blocks of copper. Every box in a run hall also wears a
 * printed label with its name. Those are drawn here as textures on the front
 * and rear of each device's box, from the device's make and model, with its
 * health LED reading its state. The colour that carries meaning (the
 * device's category, or its alarm) is on the rack ears at the front, the two
 * strips a real device bolts to the rails by, so no faceplate hides it.
 *
 * Layouts are per model family, not per SKU: the import names a model but
 * carries no front-panel drawing, so the bay count and the port mix are read
 * off the model name.
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

export type Family = 'dell' | 'hpe' | 'lenovo' | 'supermicro' | 'ibm' | 'cisco' | 'paloalto' | 'f5'
  | 'apc' | 'raritan' | 'generic';

/** The maker, from however the inventory spells it. */
export function family(vendor?: string | null): Family {
  const v = (vendor ?? '').toLowerCase();
  if (v.includes('dell')) return 'dell';
  if (v.includes('hewlett') || v.startsWith('hp')) return 'hpe';
  if (v.includes('lenovo')) return 'lenovo';
  if (v.includes('supermicro')) return 'supermicro';
  if (v.includes('ibm')) return 'ibm';
  if (v.includes('cisco')) return 'cisco';
  if (v.includes('palo alto')) return 'paloalto';
  if (v.startsWith('f5')) return 'f5';
  if (v.includes('apc') || v.includes('schneider')) return 'apc';
  if (v.includes('raritan')) return 'raritan';
  return 'generic';
}

/** The health LED's state: a device's status and worst open condition, as the
 *  front panel of a real box shows it. */
type Tier = 'ok' | 'warn' | 'crit' | 'off';
function tierOf(d: TwinDevice): Tier {
  if (d.status === 'OFFLINE') return 'off';
  if (d.max_severity === 'CRITICAL' || d.max_severity === 'MAJOR') return 'crit';
  if (d.max_severity === 'MINOR' || d.max_severity === 'WARNING') return 'warn';
  return 'ok';
}

const INK = {
  face: '#1c2025', bay: '#0c0f12', bayRim: '#4b535d', port: '#07090b', portRim: '#5a626c',
  vent: '#2b3138', ventHole: '#0d1013', cage: '#9aa3ad', label: '#e9ebee', labelInk: '#15181c',
  green: '#3ddc84', amber: '#f0a020', red: '#ff4040', blue: '#4ea3ff', cyan: '#2bc4d8', dark: '#3a4048',
};

/** Each maker's chassis finish and badge. Physical colours, the same in both
 *  themes: this is a picture of the hardware, not UI. */
const BRAND: Record<Family, { face: string; carrier: string; carrierRim: string; mark: string; word: string; ink: string }> = {
  dell:       { face: '#111417', carrier: '#0d1013', carrierRim: '#2c3238', mark: '#007db8', word: 'DELL', ink: '#ffffff' },
  hpe:        { face: '#1b1f23', carrier: '#3b4147', carrierRim: '#6a727b', mark: '#01a982', word: 'HPE', ink: '#ffffff' },
  lenovo:     { face: '#1a1d21', carrier: '#14171b', carrierRim: '#3a4047', mark: '#e2231a', word: 'Lenovo', ink: '#ffffff' },
  supermicro: { face: '#1d2024', carrier: '#2a2f35', carrierRim: '#4b535d', mark: '#1f3a5f', word: 'SUPERMICRO', ink: '#e6e9ec' },
  ibm:        { face: '#121416', carrier: '#0d1013', carrierRim: '#32383f', mark: '#0f62fe', word: 'IBM', ink: '#ffffff' },
  cisco:      { face: '#22262b', carrier: '#0c0f12', carrierRim: '#4b535d', mark: '#049fd9', word: 'cisco', ink: '#ffffff' },
  paloalto:   { face: '#262a2e', carrier: '#0c0f12', carrierRim: '#4b535d', mark: '#fa582d', word: 'paloalto', ink: '#ffffff' },
  f5:         { face: '#15181b', carrier: '#0c0f12', carrierRim: '#4b535d', mark: '#e4002b', word: 'f5', ink: '#ffffff' },
  apc:        { face: '#16191d', carrier: '#0c0f12', carrierRim: '#4b535d', mark: '#3dcd58', word: 'APC', ink: '#ffffff' },
  raritan:    { face: '#16191d', carrier: '#0c0f12', carrierRim: '#4b535d', mark: '#0071bc', word: 'Raritan', ink: '#ffffff' },
  generic:    { face: INK.face, carrier: INK.bay, carrierRim: INK.bayRim, mark: '#5a626c', word: '', ink: '#d9dde2' },
};

function seeded(seed: number): () => number {
  let s = seed >>> 0;
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
}

const PX_PER_U = 88, WIDTH = 1024;
const FONT = '"Segoe UI", system-ui, -apple-system, sans-serif';

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

// ---------------------------------------------------------------- primitives

function frame(c: CanvasRenderingContext2D, w: number, h: number, face = INK.face) {
  c.fillStyle = face; c.fillRect(0, 0, w, h);
  // a brushed hint: faint horizontal grain so a flat plate does not read as paint
  c.fillStyle = 'rgba(255,255,255,0.025)';
  for (let y = 3; y < h; y += 6) c.fillRect(0, y, w, 1);
  c.strokeStyle = '#0a0c0e'; c.lineWidth = 3; c.strokeRect(1.5, 1.5, w - 3, h - 3);
  // the top edge catches the light, the bottom falls into shadow
  c.fillStyle = 'rgba(255,255,255,0.08)'; c.fillRect(3, 3, w - 6, 2);
  c.fillStyle = 'rgba(0,0,0,0.35)'; c.fillRect(3, h - 5, w - 6, 2);
}
function led(c: CanvasRenderingContext2D, x: number, y: number, r: number, col: string) {
  c.fillStyle = col; c.beginPath(); c.arc(x, y, r, 0, Math.PI * 2); c.fill();
  if (col !== INK.dark && col !== '#3a4048') {
    c.fillStyle = col; c.globalAlpha = 0.35; c.beginPath(); c.arc(x, y, r * 2, 0, Math.PI * 2); c.fill(); c.globalAlpha = 1;
  }
}
function vent(c: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, hex = false) {
  c.fillStyle = INK.vent; c.fillRect(x, y, w, h);
  c.fillStyle = INK.ventHole;
  const s = Math.max(5, Math.min(9, h / 6));
  for (let yy = y + s / 2; yy < y + h - s / 2; yy += s * (hex ? 0.87 : 1)) {
    for (let xx = x + s / 2 + ((Math.round((yy - y) / s)) % 2) * (s / 2); xx < x + w - s / 2; xx += s) {
      if (hex) { c.beginPath(); c.moveTo(xx, yy - s * 0.32); for (let k = 1; k < 6; k++) c.lineTo(xx + s * 0.32 * Math.sin(k * Math.PI / 3), yy - s * 0.32 * Math.cos(k * Math.PI / 3)); c.fill(); }
      else { c.beginPath(); c.arc(xx, yy, s * 0.3, 0, Math.PI * 2); c.fill(); }
    }
  }
}
function port(c: CanvasRenderingContext2D, x: number, y: number, w: number, h: number) {
  c.fillStyle = INK.port; c.fillRect(x, y, w, h);
  c.strokeStyle = INK.portRim; c.lineWidth = 1.5; c.strokeRect(x, y, w, h);
  // the latch clip of an RJ45 jack
  c.fillStyle = '#2a3036'; c.fillRect(x + w * 0.3, y + h - 4, w * 0.4, 3);
}
function cage(c: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, lit: string | null) {
  c.fillStyle = '#05070a'; c.fillRect(x, y, w, h);
  c.strokeStyle = INK.cage; c.lineWidth = 2; c.strokeRect(x, y, w, h);
  c.fillStyle = '#6f7983'; c.fillRect(x + 2, y + h * 0.35, w - 4, 2);     // the module's pull tab
  if (lit) led(c, x + w / 2, y - 5, 2, lit);
}
function text(c: CanvasRenderingContext2D, s: string, x: number, y: number, size: number, col: string, weight = '700', align: CanvasTextAlign = 'left') {
  c.font = `${weight} ${size}px ${FONT}`; c.fillStyle = col; c.textAlign = align; c.textBaseline = 'middle';
  c.fillText(s, x, y);
}

/** The maker's badge, where the makers put theirs. */
function badge(c: CanvasRenderingContext2D, fam: Family, x: number, y: number, h: number) {
  const b = BRAND[fam];
  switch (fam) {
    case 'dell': {
      const r = Math.min(h * 0.28, 15);
      c.fillStyle = b.mark; c.beginPath(); c.arc(x + r, y, r, 0, Math.PI * 2); c.fill();
      text(c, 'DELL', x + r, y + 0.5, r * 0.78, '#ffffff', '800', 'center');
      break;
    }
    case 'hpe': {
      const bh = Math.min(h * 0.34, 20), bw = bh * 2.6;
      c.fillStyle = b.mark; c.fillRect(x, y - bh / 2, bw, bh);
      c.fillStyle = b.face; c.fillRect(x + 3, y - bh / 2 + 3, bw - 6, bh - 6);
      text(c, 'HPE', x + bw / 2, y + 0.5, bh * 0.62, '#ffffff', '800', 'center');
      break;
    }
    case 'lenovo': {
      const bh = Math.min(h * 0.32, 18), bw = bh * 3.4;
      c.fillStyle = b.mark; c.fillRect(x, y - bh / 2, bw, bh);
      text(c, 'Lenovo', x + bw / 2, y + 0.5, bh * 0.7, '#ffffff', '600', 'center');
      break;
    }
    case 'ibm': {
      // the eight-bar wordmark, as stripes through the letters
      const bh = Math.min(h * 0.36, 20);
      text(c, 'IBM', x, y, bh * 1.2, b.mark, '900', 'left');
      c.fillStyle = b.face;
      for (let k = 0; k < 4; k++) c.fillRect(x - 2, y - bh * 0.55 + k * (bh * 0.3) + bh * 0.1, bh * 2.3, 2);
      break;
    }
    case 'cisco': {
      // the bridge bars over the word
      const bh = Math.min(h * 0.3, 16);
      const bars = [0.4, 0.7, 1, 0.7, 0.4, 1, 0.7, 0.4, 1];
      bars.forEach((f, k) => { c.fillStyle = b.mark; c.fillRect(x + k * 4, y - bh * 0.95 - bh * 0.55 * f + bh * 0.55, 2.5, bh * 0.55 * f); });
      text(c, 'cisco', x, y + bh * 0.35, bh, '#d9dde2', '600', 'left');
      break;
    }
    case 'paloalto': {
      const s = Math.min(h * 0.3, 16);
      c.fillStyle = b.mark; c.fillRect(x, y - s / 2, s, s);
      text(c, 'paloalto', x + s + 6, y + 1, s * 0.9, '#d9dde2', '600', 'left');
      break;
    }
    case 'f5': {
      const r = Math.min(h * 0.26, 14);
      c.fillStyle = b.mark; c.beginPath(); c.arc(x + r, y, r, 0, Math.PI * 2); c.fill();
      text(c, 'f5', x + r, y + 1, r * 1.1, '#ffffff', '800', 'center');
      break;
    }
    case 'supermicro': {
      text(c, 'SUPERMICRO', x, y, Math.min(h * 0.22, 12), b.ink, '700', 'left');
      break;
    }
    case 'apc': case 'raritan': {
      text(c, b.word, x, y, Math.min(h * 0.26, 13), b.mark, '800', 'left');
      break;
    }
    default: break;
  }
}

/** The printed label every box in a run hall wears: the device's name on a
 *  white strip. */
function nameLabel(c: CanvasRenderingContext2D, name: string, x: number, y: number, w: number, h: number) {
  c.fillStyle = INK.label; c.fillRect(x, y, w, h);
  c.strokeStyle = 'rgba(0,0,0,0.5)'; c.lineWidth = 1; c.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
  c.save(); c.beginPath(); c.rect(x + 3, y, w - 6, h); c.clip();
  text(c, name, x + w / 2, y + h / 2 + 1, Math.min(h * 0.62, 15), INK.labelInk, '600', 'center');
  c.restore();
}

function healthLeds(c: CanvasRenderingContext2D, tier: Tier, x: number, y: number, h: number, column = true) {
  const dead = tier === 'off';
  const health = tier === 'crit' ? INK.red : tier === 'warn' ? INK.amber : dead ? INK.dark : INK.green;
  const cols = [dead ? INK.dark : INK.green, health, dead ? INK.dark : INK.green, dead ? INK.dark : INK.blue];
  const step = column ? Math.min(14, h * 0.2) : 14;
  cols.forEach((col, k) => led(c, column ? x : x + k * step, column ? y + (k - 1.5) * step : y, 3, col));
}

// ---------------------------------------------------------------- servers

/** How many front bays the model carries, read off its name. */
function bayCount(fam: Family, model: string, u: number): number {
  const m = model.toLowerCase();
  if (fam === 'dell') return u >= 2 ? (/r7525|r760|r750/.test(m) ? 24 : 16) : 10;
  if (fam === 'hpe') return u >= 2 ? (/gen11/.test(m) ? 16 : 8) : 8;
  if (fam === 'lenovo') return u >= 2 ? 24 : 10;
  if (fam === 'supermicro') return u >= 2 ? 24 : 12;
  if (fam === 'ibm') return 8;
  return u >= 2 ? 16 : 8;
}

/** One hot-swap carrier, in the maker's style. */
function carrier(c: CanvasRenderingContext2D, fam: Family, x: number, y: number, w: number, h: number, on: boolean, rnd: () => number) {
  const b = BRAND[fam];
  c.fillStyle = b.carrier; c.fillRect(x, y, w, h);
  c.strokeStyle = b.carrierRim; c.lineWidth = 1.5; c.strokeRect(x, y, w, h);
  const act = on && rnd() > 0.12;
  switch (fam) {
    case 'dell':
      // a thin status strip down the latch, lit at the foot
      c.fillStyle = '#23282e'; c.fillRect(x + 2, y + h - 10, w - 4, 7);
      led(c, x + w * 0.5, y + h - 6.5, 2, on ? (act ? INK.green : INK.dark) : INK.dark);
      break;
    case 'hpe':
      // the Smart Carrier: a wide bar at the top that lights 'do not remove'
      c.fillStyle = '#20252a'; c.fillRect(x + 2, y + 2, w - 4, 6);
      if (on) { c.fillStyle = INK.green; c.fillRect(x + 3, y + 3, (w - 6) * 0.45, 4); }
      c.fillStyle = '#5a626c'; c.fillRect(x + w * 0.2, y + h - 9, w * 0.6, 3);   // the release handle
      break;
    case 'lenovo':
      // the orange-red latch tab at the foot
      c.fillStyle = BRAND.lenovo.mark; c.fillRect(x + 2, y + h - 9, w * 0.45, 6);
      led(c, x + w * 0.8, y + h - 6, 2, on ? (act ? INK.green : INK.dark) : INK.dark);
      break;
    case 'supermicro':
      // perforated face, green hot-swap tab with its two lights
      vent(c, x + 2, y + 2, w - 4, h - 14);
      c.fillStyle = '#2f8f4a'; c.fillRect(x + 2, y + h - 10, w * 0.4, 7);
      led(c, x + w * 0.65, y + h - 6.5, 1.8, on ? INK.blue : INK.dark);
      led(c, x + w * 0.85, y + h - 6.5, 1.8, INK.dark);
      break;
    case 'ibm':
      led(c, x + w * 0.5, y + h - 7, 2, on ? INK.green : INK.dark);
      c.fillStyle = '#2a3036'; c.fillRect(x + 2, y + 2, w - 4, 3);
      break;
    default:
      led(c, x + w / 2, y + h * 0.8, Math.max(1.5, w * 0.08), on ? (act ? INK.green : INK.amber) : INK.dark);
  }
}

function serverFront(fam: Family, model: string, u: number, tier: Tier, name: string | null) {
  return canvas(u, (c, w, h) => {
    const b = BRAND[fam];
    frame(c, w, h, b.face);
    const rnd = seeded(7 + u + model.length);
    const on = tier !== 'off';
    const bays = bayCount(fam, model, u);
    // the bay zone, by maker: Dell and Lenovo keep the control panel on the
    // left ear, HPE and Supermicro keep the lights on the right
    const ctrlLeft = fam === 'dell' || fam === 'lenovo' || fam === 'ibm';
    const x0 = ctrlLeft ? w * 0.085 : w * 0.03;
    const bayW = w * (fam === 'ibm' ? 0.34 : 0.6);
    if (u === 1 || fam === 'ibm' || bays <= 12) {
      // small-form-factor carriers standing on end, in the maker's groups
      const groups = fam === 'lenovo' ? (bays === 10 ? [4, 4, 2] : [8, 8, 8]) : fam === 'hpe' ? [4, 4] : fam === 'ibm' ? [4, 4] : [bays];
      const gap = 6, n = groups.reduce((a, g) => a + g, 0);
      const bw = (bayW - gap * (groups.length - 1)) / n;
      let x = x0;
      for (const g of groups) {
        for (let i = 0; i < g; i++) { carrier(c, fam, x + 1, h * 0.1, bw - 2, h * 0.8, on, rnd); x += bw; }
        x += gap;
      }
      if (fam === 'hpe' && u >= 2) {
        // Gen10: the second and third cages are blank fillers
        c.fillStyle = '#23282d'; c.fillRect(x0 + bayW + 8, h * 0.1, w * 0.18, h * 0.8);
        vent(c, x0 + bayW + 12, h * 0.14, w * 0.17, h * 0.72);
      }
      if (fam === 'ibm') {
        // the DVD bay and the op panel, then the perforated bezel the badge sits on
        c.fillStyle = '#0d1013'; c.fillRect(x0 + bayW + 8, h * 0.12, w * 0.1, h * 0.22);
        c.fillStyle = '#04161a'; c.fillRect(x0 + bayW + 8, h * 0.42, w * 0.1, h * 0.3);
        text(c, 'A1', x0 + bayW + 8 + w * 0.05, h * 0.57, 11, INK.cyan, '600', 'center');
        vent(c, w * 0.52, h * 0.1, w * 0.36, h * 0.8, true);
      }
    } else {
      // 2U: carriers two or three to a column, on end, across the full zone
      const bw = bayW / bays;
      for (let i = 0; i < bays; i++) carrier(c, fam, x0 + i * bw + 1, h * 0.08, bw - 2, h * 0.84, on, rnd);
    }
    // the rest of the face: the maker's vent pattern
    if (fam !== 'ibm') {
      const vx = x0 + bayW + (fam === 'hpe' && u >= 2 ? w * 0.19 : 0) + 8;
      const vw = w * 0.885 - vx;
      if (vw > 20) vent(c, vx, h * 0.12, vw, h * 0.76, fam === 'dell');
    }
    // control panel and lights
    if (ctrlLeft) {
      // power button with its ring, the ID button in the maker's blue, the pull-out tag
      led(c, w * 0.042, h * 0.3, Math.min(8, h * 0.11), '#3a4048');
      led(c, w * 0.042, h * 0.3, Math.min(4.5, h * 0.06), on ? INK.green : INK.dark);
      led(c, w * 0.042, h * 0.62, Math.min(5, h * 0.07), fam === 'dell' ? BRAND.dell.mark : fam === 'lenovo' ? INK.blue : INK.dark);
      if (fam === 'lenovo') { c.fillStyle = '#04161a'; c.fillRect(w * 0.02, h * 0.78, w * 0.045, Math.max(5, h * 0.12)); }
      healthLeds(c, tier, w * 0.925, h * 0.5, h);
    } else {
      healthLeds(c, tier, w * 0.93, h * 0.5, h);
      led(c, w * 0.965, h * 0.5, Math.min(6, h * 0.09), '#3a4048');
      led(c, w * 0.965, h * 0.5, Math.min(3.5, h * 0.05), on ? INK.green : INK.dark);
    }
    badge(c, fam, fam === 'hpe' ? w * 0.9 - 40 : fam === 'supermicro' ? w * 0.70 : ctrlLeft ? w * 0.012 : w * 0.76,
          fam === 'dell' || fam === 'lenovo' || fam === 'ibm' ? h - Math.min(h * 0.3, 16) : h * 0.5, h);
    if (name) nameLabel(c, name, w * 0.735, 6, w * 0.15, Math.min(24, h * 0.3));
  });
}

// ---------------------------------------------------------------- network

/** What the model's front carries. */
type Ports = 'sfp48' | 'qsfp64' | 'rj45x48' | 'router' | 'firewall' | 'lb';
function portsOf(kind: Kind, fam: Family, model: string): Ports {
  const m = model.toLowerCase();
  if (/9364|z9264/.test(m)) return 'qsfp64';
  if (/93180|s5248/.test(m)) return 'sfp48';
  if (/catalyst|n1148|n3248|9300|-48t/.test(m)) return 'rj45x48';
  if (/pa-|palo/.test(m) || fam === 'paloalto') return 'firewall';
  if (/big-ip|i5800/.test(m) || fam === 'f5') return 'lb';
  if (/asr|isr/.test(m)) return 'router';
  return kind === 'switch' ? 'rj45x48' : 'router';
}

function networkFront(fam: Family, model: string, u: number, tier: Tier, name: string | null, kind: Kind) {
  return canvas(u, (c, w, h) => {
    const b = BRAND[fam];
    frame(c, w, h, b.face);
    const rnd = seeded(31 + model.length);
    const on = tier !== 'off';
    const lit = () => (on ? (rnd() > 0.3 ? INK.green : rnd() > 0.5 ? INK.amber : null) : null);
    const ports = portsOf(kind, fam, model);
    const rowH = Math.min(h * 0.3, 24);
    switch (ports) {
      case 'sfp48': {
        // 48 SFP28 in two rows of 24, cages in pairs; 6 QSFP uplinks at the right
        const x0 = w * 0.1, pw = (w * 0.6) / 24;
        for (let i = 0; i < 24; i++) {
          const x = x0 + i * pw + Math.floor(i / 4) * 3;
          for (let r = 0; r < 2; r++) cage(c, x + 2, h * 0.5 - rowH - 3 + r * (rowH + 6), pw - 5, rowH, lit());
        }
        for (let i = 0; i < 6; i++) {
          const qw = w * 0.034, x = w * 0.75 + (i % 3) * (qw + 6), y = h * 0.5 - rowH - 3 + Math.floor(i / 3) * (rowH + 6);
          cage(c, x, y, qw, rowH, i < 2 && on ? INK.green : null);
        }
        break;
      }
      case 'qsfp64': {
        const x0 = w * 0.08, cols = 32, qw = (w * 0.86) / cols;
        for (let i = 0; i < 64; i++) {
          const x = x0 + (i % cols) * qw + Math.floor((i % cols) / 4) * 2, y = h * 0.5 - rowH * 1.1 + Math.floor(i / cols) * (rowH * 1.2 + 6);
          cage(c, x + 2, y, qw - 5, rowH * 1.1, i % 7 === 0 && on ? INK.green : null);
        }
        break;
      }
      case 'rj45x48': {
        // 48 copper in four blocks of twelve, link lights above; 4 SFP uplinks
        const x0 = w * 0.1, pw = (w * 0.62) / 24, ph = Math.min(rowH, pw * 0.85);
        for (let i = 0; i < 24; i++) {
          const x = x0 + i * pw + Math.floor(i / 6) * 4;
          for (let r = 0; r < 2; r++) port(c, x + 2, h * 0.5 - ph - 2 + r * (ph + 4), pw - 5, ph);
          if (on && rnd() > 0.3) led(c, x + pw * 0.3, h * 0.5 - ph - 8, 2, INK.green);
          if (on && rnd() > 0.6) led(c, x + pw * 0.7, h * 0.5 - ph - 8, 2, INK.amber);
        }
        for (let i = 0; i < 4; i++) cage(c, w * 0.78 + i * (w * 0.045), h * 0.28, w * 0.036, h * 0.44, i < 2 && on ? INK.green : null);
        break;
      }
      case 'router': {
        // SFP and copper WAN ports left, NIM bays as blank panels across the middle
        for (let i = 0; i < 6; i++) cage(c, w * 0.1 + i * (w * 0.04), h * 0.3, w * 0.034, h * 0.4, i < 2 && on ? INK.green : null);
        for (let i = 0; i < 2; i++) port(c, w * 0.36 + i * (w * 0.045), h * 0.32, w * 0.038, h * 0.36);
        for (let i = 0; i < 2; i++) {
          c.fillStyle = '#2b3036'; c.fillRect(w * 0.48 + i * w * 0.2, h * 0.14, w * 0.18, h * 0.72);
          c.strokeStyle = '#4b535d'; c.lineWidth = 2; c.strokeRect(w * 0.48 + i * w * 0.2, h * 0.14, w * 0.18, h * 0.72);
          text(c, `NIM ${i}`, w * 0.57 + i * w * 0.2, h * 0.5, 11, '#8a939d', '600', 'center');
        }
        break;
      }
      case 'firewall': {
        // management and console at the left, then 12 copper, 4 SFP, 4 SFP+; the rest is a vented bezel
        port(c, w * 0.035, h * 0.18, w * 0.03, h * 0.26); port(c, w * 0.035, h * 0.56, w * 0.03, h * 0.26);
        const pw = w * 0.036, ph = Math.min(rowH, pw * 0.8);
        for (let i = 0; i < 12; i++) port(c, w * 0.1 + i * (pw + 4), h * 0.5 - ph, pw, ph);
        for (let i = 0; i < 8; i++) cage(c, w * 0.1 + 12 * (pw + 4) + 10 + i * (w * 0.038), h * 0.5 - ph, w * 0.032, ph, i < 3 && on ? INK.green : null);
        vent(c, w * 0.1, h * 0.56, w * 0.74, h * 0.3);
        break;
      }
      case 'lb': {
        // the LCD with its four buttons on the left, 8 SFP+ and 4 QSFP+
        c.fillStyle = '#04161a'; c.fillRect(w * 0.03, h * 0.2, w * 0.1, h * 0.6);
        c.fillStyle = INK.cyan; c.fillRect(w * 0.04, h * 0.32, w * 0.07, Math.max(2, h * 0.09)); c.fillRect(w * 0.04, h * 0.52, w * 0.05, Math.max(2, h * 0.09));
        for (let i = 0; i < 4; i++) led(c, w * 0.145 + i * 12, h * 0.5, 3.5, '#3a4048');
        for (let i = 0; i < 8; i++) cage(c, w * 0.24 + i * (w * 0.042), h * 0.3, w * 0.036, h * 0.4, i < 4 && on ? INK.green : null);
        for (let i = 0; i < 4; i++) cage(c, w * 0.6 + i * (w * 0.05), h * 0.26, w * 0.044, h * 0.48, i < 1 && on ? INK.green : null);
        break;
      }
    }
    // status lights and the console at the far left, the badge below them
    healthLeds(c, tier, w * 0.05, h * 0.3, h, false);
    badge(c, fam, w * 0.02, h * 0.72, h);
    if (name) nameLabel(c, name, w * 0.86, 6, w * 0.125, Math.min(22, h * 0.28));
  });
}

function genericFront(u: number, tier: Tier, name: string | null) {
  return canvas(u, (c, w, h) => {
    frame(c, w, h);
    vent(c, w * 0.06, h * 0.14, w * 0.66, h * 0.72);
    c.fillStyle = '#04161a'; c.fillRect(w * 0.76, h * 0.25, w * 0.12, h * 0.5);
    c.fillStyle = INK.cyan; c.fillRect(w * 0.775, h * 0.38, w * 0.07, Math.max(2, h * 0.1));
    healthLeds(c, tier, w * 0.93, h * 0.5, h);
    if (name) nameLabel(c, name, w * 0.5, 6, w * 0.2, Math.min(22, h * 0.28));
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

/** A zero-U strip's outlet face: the maker's display at the top, then C13s
 *  in banks, each outlet with its own light on a metered-by-outlet strip. */
function pduFace(fam: Family, tier: Tier) {
  const b = BRAND[fam];
  const on = tier !== 'off';
  return canvas(1, (c, w, h) => {
    c.fillStyle = b.face; c.fillRect(0, 0, w, h);
    c.fillStyle = '#04161a'; c.fillRect(w * 0.12, h * 0.015, w * 0.76, h * 0.04);
    c.fillStyle = on ? (fam === 'raritan' ? INK.blue : INK.cyan) : INK.dark; c.fillRect(w * 0.2, h * 0.026, w * 0.4, h * 0.012);
    text(c, b.word || 'PDU', w * 0.5, h * 0.07, 7, b.mark, '800', 'center');
    for (let i = 0; i < 24; i++) {
      const y = h * 0.09 + i * (h * 0.88 / 24) + Math.floor(i / 8) * 3;
      c.fillStyle = '#05070a'; c.fillRect(w * 0.22, y, w * 0.5, h * 0.024);
      c.strokeStyle = '#4b535d'; c.lineWidth = 1; c.strokeRect(w * 0.22, y, w * 0.5, h * 0.024);
      led(c, w * 0.82, y + h * 0.012, 1.4, on ? (fam === 'raritan' ? INK.blue : INK.green) : INK.dark);
    }
  }, 64);
}

type Box = [number, number, number, number, number, number];

/** Front and rear faceplates and the coloured rack ears for every placed device. */
export function DeviceFaces({ devices, boxes, rackById, colors, labels = false }: {
  devices: TwinDevice[]; boxes: (Box | null)[]; rackById: Map<string, FloorRack>;
  /** Each device's meaning colour (category or alarm), resolved. */
  colors: THREE.Color[];
  /** Print each device's name on its faceplate (one texture per device, so
   *  only where a few are shown: the rack's own view). */
  labels?: boolean;
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
      const fam = family(d.vendor);
      const model = d.model ?? '';
      const tier = tierOf(d);
      const name = labels ? d.name : null;
      const face = (sign: number, key: string, make: () => THREE.CanvasTexture, fw = w, fh = h) => {
        q.setFromAxisAngle(up, sign > 0 ? 0 : Math.PI);
        put(key, make, new THREE.Matrix4().compose(p.set(x, y, z + sign * (dep / 2 + 0.0015)), q, s.set(fw, fh, 1)));
      };
      if (d.u_start != null && d.u_start > 0) {
        const u = Math.max(1, Math.min(8, Math.round(d.u_height || 1)));
        const key = `f:${kind}:${fam}:${model}:${u}:${tier}:${name ?? ''}`;
        const make = kind === 'server' ? () => serverFront(fam, model, u, tier, name)
          : kind === 'switch' || kind === 'appliance' ? () => networkFront(fam, model, u, tier, name, kind)
          : () => genericFront(u, tier, name);
        face(front, key, make, w - 0.04, h);
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
        face(-front, `pdu:${fam}:${tier}`, () => pduFace(fam, tier), w * 0.8, h * 0.98);
      }
    });
    return { faces: [...groups.entries()], ears: { mats: earMats, cols: earCols } };
  }, [devices, boxes, rackById, colors, labels]); // eslint-disable-line react-hooks/exhaustive-deps

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

// ---------------------------------------------------------------- rails

const RAIL_PX_U = 64, RAIL_W = 96;

/** A mounting rail's face: the U number beside each unit's three square
 *  cage-nut holes, numbered from the bottom as every cabinet is. */
function railTexture(units: number): THREE.CanvasTexture {
  const cv = document.createElement('canvas');
  cv.width = RAIL_W; cv.height = units * RAIL_PX_U;
  const c = cv.getContext('2d') as CanvasRenderingContext2D;
  c.fillStyle = '#4a515a'; c.fillRect(0, 0, cv.width, cv.height);
  c.fillStyle = 'rgba(255,255,255,0.06)'; c.fillRect(0, 0, 3, cv.height);
  c.fillStyle = 'rgba(0,0,0,0.3)'; c.fillRect(cv.width - 3, 0, 3, cv.height);
  for (let u = 1; u <= units; u++) {
    const top = cv.height - u * RAIL_PX_U;
    for (const f of [0.17, 0.5, 0.83]) {
      c.fillStyle = '#15181c'; c.fillRect(RAIL_W * 0.62, top + RAIL_PX_U * f - 6, 12, 12);
    }
    c.fillStyle = '#0b0d10'; c.fillRect(0, top, cv.width, 1);
    text(c, String(u), RAIL_W * 0.3, top + RAIL_PX_U / 2, 20, '#d9dde2', '600', 'center');
  }
  const t = new THREE.CanvasTexture(cv);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}

/** The four mounting rails of one cabinet, numbered, inside the frame at the
 *  front and rear door lines, for the rack's own view. */
export function RackRails({ box, units }: { box: { x: number; y: number; z: number; w: number; h: number; d: number; front: number }; units: number }) {
  const tex = useMemo(() => railTexture(units), [units]);
  useEffect(() => () => tex.dispose(), [tex]);
  const h = units * U;
  const y = RACK_BASE + h / 2;
  const railW = 0.034;
  const f = box.front || 1;
  // a device's face sits dep/2 - 0.06 from the cabinet centre (deviceBox);
  // the rails stand just proud of that so the ears bolt onto them
  const zf = box.z + f * (box.d / 2 - 0.062), zr = box.z - f * (box.d / 2 - 0.062);
  const rails = [
    { x: box.x - (box.w / 2 - 0.047), z: zf, rot: f > 0 ? 0 : Math.PI },
    { x: box.x + (box.w / 2 - 0.047), z: zf, rot: f > 0 ? 0 : Math.PI },
    { x: box.x - (box.w / 2 - 0.047), z: zr, rot: f > 0 ? Math.PI : 0 },
    { x: box.x + (box.w / 2 - 0.047), z: zr, rot: f > 0 ? Math.PI : 0 },
  ];
  return (
    <group>
      {rails.map((r, i) => (
        <mesh key={i} position={[r.x, y, r.z]} rotation-y={r.rot} raycast={() => null}>
          <planeGeometry args={[railW, h]} />
          <meshBasicMaterial map={tex} toneMapped={false} />
        </mesh>
      ))}
    </group>
  );
}
