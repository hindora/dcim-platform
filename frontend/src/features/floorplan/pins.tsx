/**
 * Alarm pins: the marker the 3D views stand on whatever raised an open alarm.
 *
 * Shared by every rung - the compound and the building (BuildingScene), the
 * room and the rack on its own (Scene) - so a pin means the same thing, looks
 * the same and pulses the same way at every level of the twin.
 */

import { useThree } from '@react-three/fiber';
import { useEffect, useMemo, useRef } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import * as THREE from 'three';
import type { AlarmCategory } from '../../api/client';
import { CategoryGlyph } from '../../components/CategoryGlyph';

/** One alarm marker: a map pin whose tip is at (x, y, z) - the top centre of
 *  the device that raised it. */
export interface Pin { x: number; y: number; z: number; c: string; cat: AlarmCategory | null; n: number }

/** One alarm marker as an image: a map pin - a teardrop in the severity
 *  colour, a dark outline so it reads on the white tile and the dark studio,
 *  and a white disc holding the category's glyph (the same icons as Home's
 *  indicator strip; a bell when the category is not known) - with the count
 *  in a bubble when more than one is open. The tip is the image's bottom
 *  centre, which is where the sprite is anchored. */
const PIN_W = 128, PIN_H = 168;
/** The category glyph's size inside the pin's white disc (r 39), px of the texture. */
const GLYPH = 56;
/** A sprite's anchor at the pin's tip: bottom centre. */
const PIN_TIP = new THREE.Vector2(0.5, 0);
function pinTexture(cat: AlarmCategory | null, colour: string, n: number, onReady: () => void): THREE.CanvasTexture {
  const cv = document.createElement('canvas');
  cv.width = PIN_W; cv.height = PIN_H;
  const c = cv.getContext('2d') as CanvasRenderingContext2D;
  const tex = new THREE.CanvasTexture(cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  const cx = PIN_W / 2, cy = 60, R = 50;
  const body = () => {
    c.clearRect(0, 0, PIN_W, PIN_H);
    // the teardrop: the head's circle, its sides running tangent down to the tip
    const tipY = PIN_H - 4, a = Math.acos(R / (tipY - cy));   // where the sides run tangent to the head
    c.beginPath();
    c.arc(cx, cy, R, Math.PI / 2 + a, Math.PI / 2 - a);
    c.lineTo(cx, tipY);
    c.closePath();
    c.fillStyle = colour; c.fill();
    c.strokeStyle = 'rgba(10,12,16,0.9)'; c.lineWidth = 5; c.lineJoin = 'round'; c.stroke();
    c.fillStyle = '#ffffff'; c.beginPath(); c.arc(cx, cy, 39, 0, Math.PI * 2); c.fill();
  };
  const count = () => {
    if (n <= 1) return;
    const t = n > 9 ? '9+' : String(n);
    c.fillStyle = '#0d1117'; c.beginPath(); c.arc(PIN_W - 22, 22, 20, 0, Math.PI * 2); c.fill();
    c.strokeStyle = '#ffffff'; c.lineWidth = 3; c.stroke();
    c.fillStyle = '#ffffff'; c.font = 'bold 22px system-ui, sans-serif'; c.textAlign = 'center'; c.textBaseline = 'middle';
    c.fillText(t, PIN_W - 22, 23);
  };
  body(); count();
  // Only the namespace and the ink are added: most glyphs set their own
  // stroke-width on the root, and a second one there is a duplicate attribute
  // - invalid SVG, the image never loads, and the pin shows an empty disc.
  const svg = renderToStaticMarkup(<CategoryGlyph kind={cat ?? 'alarms'} size={GLYPH} />)
    .replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg" color="#14181e"');
  const img = new Image();
  img.onload = () => { body(); c.drawImage(img, cx - GLYPH / 2, cy - GLYPH / 2, GLYPH, GLYPH); count(); tex.needsUpdate = true; onReady(); };
  img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  return tex;
}

/** Alarm markers: a map pin standing on whatever raised it - severity by
 *  colour, what kind of alarm by its icon. The same size on screen at any
 *  distance and drawn over the glass, so a far-off alarm is never lost. The pins pulse
 *  (1.2 s, a soft fade, not a flash), and hold still under reduced motion; the
 *  scene only repaints while a badge is on screen. */
export function AlarmPins({ pins, size = 0.054 }: { pins: Pin[]; /** Height as a share of the viewport. */ size?: number }) {
  const group = useRef<THREE.Group>(null);
  const { invalidate } = useThree();
  const textures = useMemo(() => new Map<string, THREE.CanvasTexture>(), []);
  useEffect(() => () => { textures.forEach((t) => t.dispose()); textures.clear(); }, [textures]);
  const texFor = (p: Pin) => {
    const key = `${p.cat ?? 'alarms'}|${p.c}|${Math.min(p.n, 10)}`;
    let t = textures.get(key);
    if (!t) { t = pinTexture(p.cat, p.c, p.n, invalidate); textures.set(key, t); }
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
  // Anchored at its bottom centre (the pin's tip), so the tip stays on the
  // device's top centre from any angle and the pin cannot drift onto a neighbour.
  return (
    <group ref={group}>
      {pins.map((p, i) => (
        <sprite key={i} position={[p.x, p.y, p.z]} center={PIN_TIP} renderOrder={20}
                scale={[size * (PIN_W / PIN_H), size, 1]} raycast={() => null}>
          <spriteMaterial map={texFor(p)} sizeAttenuation={false} depthTest={false} transparent />
        </sprite>
      ))}
    </group>
  );
}

/** One anomaly marker: findings shown on a unit instead of raised as alarms.
 *  `beside` puts it to the right of an alarm pin on the same unit, so the two
 *  never cover each other. */
export interface AnomalyMark { x: number; y: number; z: number; n: number; beside: boolean }

const MARK = 96;
function markTexture(colour: string, n: number): THREE.CanvasTexture {
  const cv = document.createElement('canvas');
  cv.width = MARK; cv.height = MARK;
  const c = cv.getContext('2d') as CanvasRenderingContext2D;
  const cx = MARK / 2, cy = MARK / 2;
  // A disc, not the alarm's teardrop: a different shape says "different kind
  // of thing" without relying on colour.
  c.beginPath(); c.arc(cx, cy, 40, 0, Math.PI * 2);
  c.fillStyle = '#ffffff'; c.fill();
  c.lineWidth = 7; c.strokeStyle = colour; c.stroke();
  c.lineWidth = 3; c.strokeStyle = 'rgba(10,12,16,0.85)';
  c.beginPath(); c.arc(cx, cy, 44, 0, Math.PI * 2); c.stroke();
  // The trend line: up, dip, up - "running out of pattern".
  c.strokeStyle = colour; c.lineWidth = 7; c.lineCap = 'round'; c.lineJoin = 'round';
  c.beginPath();
  c.moveTo(cx - 22, cy + 12); c.lineTo(cx - 6, cy - 6); c.lineTo(cx + 4, cy + 4); c.lineTo(cx + 20, cy - 14);
  c.stroke();
  c.beginPath(); c.moveTo(cx + 8, cy - 15); c.lineTo(cx + 21, cy - 15); c.lineTo(cx + 21, cy - 3); c.stroke();
  if (n > 1) {
    const t = n > 9 ? '9+' : String(n);
    c.fillStyle = '#0d1117'; c.beginPath(); c.arc(MARK - 17, 17, 16, 0, Math.PI * 2); c.fill();
    c.strokeStyle = '#ffffff'; c.lineWidth = 3; c.stroke();
    c.fillStyle = '#ffffff'; c.font = 'bold 18px system-ui, sans-serif'; c.textAlign = 'center'; c.textBaseline = 'middle';
    c.fillText(t, MARK - 17, 18);
  }
  const tex = new THREE.CanvasTexture(cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

const MARK_ON = new THREE.Vector2(0.5, 0);
const MARK_BESIDE = new THREE.Vector2(-0.45, -0.15);

/** Anomaly markers: steady, never pulsing - a finding asks for a look, not a
 *  response - and smaller than an alarm pin, which outranks it. */
export function AnomalyMarks({ marks, colour, size = 0.036 }: { marks: AnomalyMark[]; colour: string; size?: number }) {
  const textures = useMemo(() => new Map<string, THREE.CanvasTexture>(), []);
  useEffect(() => () => { textures.forEach((t) => t.dispose()); textures.clear(); }, [textures]);
  const texFor = (n: number) => {
    const key = `${colour}|${Math.min(n, 10)}`;
    let t = textures.get(key);
    if (!t) { t = markTexture(colour, n); textures.set(key, t); }
    return t;
  };
  return (
    <group>
      {marks.map((m, i) => (
        <sprite key={i} position={[m.x, m.y, m.z]} center={m.beside ? MARK_BESIDE : MARK_ON} renderOrder={19}
                scale={[size, size, 1]} raycast={() => null}>
          <spriteMaterial map={texFor(m.n)} sizeAttenuation={false} depthTest={false} transparent />
        </sprite>
      ))}
    </group>
  );
}
