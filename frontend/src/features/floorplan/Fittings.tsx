import { useEffect, useLayoutEffect, useMemo, useRef } from 'react';
import * as THREE from 'three';
import type { GBox } from './Scene';
import { kindOfRoom } from './furniture';

/**
 * What makes a room read as a room, and a cabinet as a cabinet, without
 * touching what the layers say (docs/27 §8e).
 *
 * Two kinds of thing are drawn here and they are kept apart:
 *
 * - **From data.** Door openings come from the imported building fabric, and a
 *   door with a BMS contact shows what the contact reads. A rack's front is
 *   the face its `facing` names; a unit's front is the face it discharges
 *   from.
 * - **By rule.** Nothing reports where an extinguisher hangs. Fire points are
 *   drawn where a fire code puts them - a handheld and a call point at every
 *   exit, a clean-agent release at the door of a room that has gas
 *   suppression, the cylinder bank against a free wall - so the room reads
 *   correctly, and the "Room fittings" switch says they are drawn by rule.
 *   They carry no state and are never coloured by a layer.
 *
 * Everything here is paint over the readings, never instead of them: the
 * detail on a cabinet is dark trim and a perforated door the layer's colour
 * shows through, and none of it answers the pointer.
 */

// Physical colours, the same under both themes - see SURF in Scene.tsx.
const FIT = {
  frame: '#3a4048', leaf: '#7d8792', leafEdge: '#4b535d', glass: '#9fc4d8', bar: '#d6dade',
  fire: '#c62a26', fireDark: '#7f1a17', hose: '#1d2126', release: '#e0b020', abort: '#2f6fb3',
  exit: '#1f9d55', exitInk: '#ffffff', skirting: '#8f96a0', plinth: '#22262c',
  desk: '#b8a58a', screen: '#15181d', steel: '#aab1ba', fence: '#7c848e', stack: '#4a5058',
};
const DOOR_H = 2.1;
const INK = '20,24,30';

/** A door opening, in room metres, with what is known about it. */
export interface RoomDoor {
  x0: number; y0: number; x1: number; y1: number;
  /** A BMS contact watches it. */
  monitored: boolean;
  /** null when no contact reports. */
  open: boolean | null;
  /** Colour of an open alarm on the contact, or null. */
  alarm: string | null;
}

/** What the room's fire instruments read; null = the room has none, and its
 *  fire points are then drawn by rule with nothing to say. */
export interface RoomFire {
  /** 0 normal, 1 Alert, 2 Action, 3 Fire 1, 4 Fire 2; null = no detector. */
  stage: number | null;
  /** 0 normal, 1 first alarm, 2 pre-discharge, 3 released, 4 abort held; null = no release panel. */
  gas: number | null;
  countdown: number | null;
  released: boolean;
}

/** The room's fire state in an operator's words, or null when there is nothing to say. */
export function fireWords(f: RoomFire | null | undefined): { text: string; level: 'ok' | 'warn' | 'critical' } | null {
  if (!f || (f.stage == null && f.gas == null)) return null;
  if (f.released || f.gas === 3) return { text: 'Agent released - room unprotected', level: 'critical' };
  if (f.gas === 2) return { text: `Gas release in ${Math.round(f.countdown ?? 0)} s - leave the room`, level: 'critical' };
  if (f.gas === 4) return { text: `Gas release held by abort at ${Math.round(f.countdown ?? 0)} s`, level: 'critical' };
  if ((f.stage ?? 0) >= 3) return { text: f.stage === 4 ? 'Smoke: Fire 2' : 'Smoke: Fire 1', level: 'critical' };
  if (f.stage === 2) return { text: 'Smoke: Action', level: 'warn' };
  if (f.stage === 1) return { text: 'Smoke: Alert', level: 'warn' };
  return { text: 'Fire: normal', level: 'ok' };
}

interface Rect { x0: number; y0: number; x1: number; y1: number }
const hits = (a: Rect, b: Rect) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;

type Wall = 'W' | 'E' | 'N' | 'S';
/** Where on which wall a door stands: the wall its opening is nearest to. */
function wallOf(d: RoomDoor, W: number, D: number): { wall: Wall; at: number; len: number } {
  const cx = (d.x0 + d.x1) / 2, cy = (d.y0 + d.y1) / 2;
  const gaps: [Wall, number][] = [['W', Math.abs(cx)], ['E', Math.abs(W - cx)], ['N', Math.abs(cy)], ['S', Math.abs(D - cy)]];
  const wall = gaps.sort((a, b) => a[1] - b[1])[0][0];
  const alongY = wall === 'W' || wall === 'E';
  return { wall, at: alongY ? cy : cx, len: Math.max(0.9, alongY ? d.y1 - d.y0 : d.x1 - d.x0) };
}

/** A frame placed on a wall: local +x runs along the wall, local +z points
 *  into the room, origin on the wall line at floor level. */
function wallFrame(wall: Wall, at: number, W: number, D: number): { pos: [number, number, number]; ry: number } {
  switch (wall) {
    case 'W': return { pos: [-W / 2, 0, at - D / 2], ry: Math.PI / 2 };
    case 'E': return { pos: [W / 2, 0, at - D / 2], ry: -Math.PI / 2 };
    case 'N': return { pos: [at - W / 2, 0, -D / 2], ry: 0 };
    default: return { pos: [at - W / 2, 0, D / 2], ry: Math.PI };
  }
}

function canvasTex(w: number, h: number, draw: (c: CanvasRenderingContext2D, w: number, h: number) => void): THREE.CanvasTexture {
  const cv = document.createElement('canvas');
  cv.width = w; cv.height = h;
  const c = cv.getContext('2d');
  if (c) draw(c, w, h);
  const t = new THREE.CanvasTexture(cv);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}

// --------------------------------------------------------------- the room

export function RoomFittings({ W, D, roomClass, roomName, doors, obstacles, fire, marks = [] }: {
  W: number; D: number; roomClass: string | null | undefined; roomName: string;
  doors: RoomDoor[];
  /** null = no fire instrument reports for this room. */
  fire: RoomFire | null;
  /** Footprints already standing in the room, room metres. */
  obstacles: Rect[];
  /** Wall-hung instruments (sensors, panels), room metres: a board or screen slides clear of them. */
  marks?: { x: number; y: number }[];
}) {
  // Gas suppression: where a release panel reports, the room has it; where
  // nothing reports, white space is assumed to, which is the rule.
  const hall = fire ? fire.gas != null : roomClass === 'white_space';
  // The sounder over each door: dark at rest, amber on a pre-alarm, red on fire or release.
  const sounder = !fire ? null
    : fire.released || (fire.gas ?? 0) >= 1 || (fire.stage ?? 0) >= 3 ? '#ff3b30'
    : (fire.stage ?? 0) >= 1 ? '#f5a623' : '#3a4048';
  const placed = useMemo(() => doors.map((d) => ({ d, ...wallOf(d, W, D) })), [doors, W, D]);

  // The cylinder bank of a gas-suppressed hall: against the first stretch of
  // wall with nothing standing in front of it and no door through it.
  const bank = useMemo(() => {
    if (!hall) return null;
    const len = 1.5, deep = 0.55;
    const keepClear: Rect[] = [...obstacles.map((o) => ({ x0: o.x0 - 0.25, y0: o.y0 - 0.25, x1: o.x1 + 0.25, y1: o.y1 + 0.25 })),
      ...doors.map((d) => ({ x0: d.x0 - 1.4, y0: d.y0 - 1.4, x1: d.x1 + 1.4, y1: d.y1 + 1.4 }))];
    const tries: { wall: Wall; span: number }[] = [{ wall: 'S', span: W }, { wall: 'N', span: W }, { wall: 'E', span: D }, { wall: 'W', span: D }];
    for (const { wall, span } of tries) {
      for (let at = len / 2 + 0.3; at <= span - len / 2 - 0.3; at += 0.3) {
        const r: Rect = wall === 'S' ? { x0: at - len / 2, x1: at + len / 2, y0: D - deep, y1: D }
          : wall === 'N' ? { x0: at - len / 2, x1: at + len / 2, y0: 0, y1: deep }
          : wall === 'E' ? { x0: W - deep, x1: W, y0: at - len / 2, y1: at + len / 2 }
          : { x0: 0, x1: deep, y0: at - len / 2, y1: at + len / 2 };
        if (!keepClear.some((k) => hits(r, k))) return { wall, at };
      }
    }
    return null;
  }, [hall, obstacles, doors, W, D]);

  // Support rooms are furnished by what they are for (drawn by rule, like the
  // fire points): the far wall faces the door, the side walls flank it.
  const furnish = roomClass === 'support' ? kindOfRoom(roomName) : null;
  const doorWall: Wall = placed[0]?.wall ?? 'W';
  const front: Wall = ({ W: 'E', E: 'W', N: 'S', S: 'N' } as const)[doorWall];
  const sides: [Wall, Wall] = front === 'N' || front === 'S' ? ['W', 'E'] : ['N', 'S'];
  const span = (w: Wall) => (w === 'N' || w === 'S' ? W : D);
  const deep = (w: Wall) => (w === 'N' || w === 'S' ? D : W);
  // Where along a wall something `len` long hangs: centred, unless an
  // instrument on that wall is in the way, then just clear of it.
  const clearAt = (w: Wall, len: number) => {
    const sp = span(w), mid = sp / 2;
    const near = marks.filter((m) => (w === 'N' ? m.y : w === 'S' ? D - m.y : w === 'W' ? m.x : W - m.x) < 0.35)
      .map((m) => (w === 'N' || w === 'S' ? m.x : m.y));
    if (!near.some((a) => Math.abs(a - mid) < len / 2 + 0.2)) return mid;
    const fits = (a: number) => a >= len / 2 + 0.1 && a <= sp - len / 2 - 0.1 && !near.some((m) => Math.abs(m - a) < len / 2 + 0.2);
    const tries = near.flatMap((m) => [m + len / 2 + 0.25, m - len / 2 - 0.25]).filter(fits);
    return tries.length ? tries.sort((a, b) => Math.abs(a - mid) - Math.abs(b - mid))[0] : mid;
  };
  const on = (w: Wall, node: React.ReactNode, len?: number) => {
    const f = wallFrame(w, len ? clearAt(w, len) : span(w) / 2, W, D);
    return <group position={f.pos} rotation-y={f.ry}>{node}</group>;
  };

  return (
    <group>
      <Skirting W={W} D={D} />
      {placed.map(({ d, wall, at, len }, i) => {
        const f = wallFrame(wall, at, W, D);
        return (
          <group key={i} position={f.pos} rotation-y={f.ry}>
            <Door len={len} door={d} />
            <FirePoint x={len / 2 + 0.45} gas={hall} counting={fire?.gas === 2 || fire?.gas === 4} />
            {sounder && (
              <mesh position={[-(len / 2 + 0.3), DOOR_H + 0.26, 0.07]} raycast={() => null}>
                <boxGeometry args={[0.16, 0.16, 0.1]} />
                <meshStandardMaterial color={sounder} emissive={sounder} emissiveIntensity={sounder === '#3a4048' ? 0 : 0.9} />
              </mesh>
            )}
          </group>
        );
      })}
      {bank && (() => {
        const f = wallFrame(bank.wall, bank.at, W, D);
        return <group position={f.pos} rotation-y={f.ry}><CylinderBank spent={Boolean(fire?.released || fire?.gas === 3)} /></group>;
      })()}
      {furnish === 'ops' && (
        <>
          {/* A NOC faces its video wall; the consoles stand back from it, one operator each. */}
          {on(front, <VideoWall w={Math.min(3.6, span(front) * 0.62)} />, Math.min(3.6, span(front) * 0.62))}
          {on(front, (() => {
            const len = Math.min(1.8, span(front) * 0.32), z = Math.min(2.2, deep(front) * 0.45);
            return [-1, 1].map((k) => (
              <group key={k} position={[k * (len / 2 + 0.15), 0, z]}>
                <Console len={len} screens={3} />
                <Chair z={0.75} />
              </group>
            ));
          })())}
          {on(sides[0], <Whiteboard len={Math.min(1.8, span(sides[0]) * 0.4)} />, Math.min(1.8, span(sides[0]) * 0.4))}
        </>
      )}
      {furnish === 'mmr' && (
        <>
          {/* A meet-me room is the carriers' demarcation, not an office: a cross-connect
              records board and a small bench for the crash-cart screen. */}
          {on(front, <Whiteboard len={Math.min(1.6, span(front) * 0.45)} />, Math.min(1.6, span(front) * 0.45))}
          {on(sides[0], <group position={[0, 0, 0.4]}><Bench len={Math.min(1.4, span(sides[0]) * 0.35)} screens={1} /><Stool z={0.75} /></group>)}
        </>
      )}
      {furnish === 'staging' && (
        <>
          {/* Spares on steel shelving, a build and burn-in bench, the job board. */}
          {on(front, <Shelving len={Math.min(4, span(front) * 0.6)} />)}
          {on(sides[0], <group position={[0, 0, 0.4]}>
            <Bench len={Math.min(2.4, span(sides[0]) * 0.5)} screens={2} />
            {[-0.5, 0.5].map((x) => <Stool key={x} x={x} z={0.75} />)}
          </group>)}
          {on(sides[1], <Whiteboard len={Math.min(1.8, span(sides[1]) * 0.4)} />, Math.min(1.8, span(sides[1]) * 0.4))}
        </>
      )}
    </group>
  );
}

function Skirting({ W, D }: { W: number; D: number }) {
  const walls: [number, number, number, number][] = [
    [0, -D / 2 + 0.006, 0, W], [0, D / 2 - 0.006, Math.PI, W], [-W / 2 + 0.006, 0, Math.PI / 2, D], [W / 2 - 0.006, 0, -Math.PI / 2, D],
  ];
  return (
    <>
      {walls.map(([x, z, ry, len], i) => (
        <mesh key={i} position={[x, 0.06, z]} rotation-y={ry} raycast={() => null}>
          <planeGeometry args={[len, 0.12]} />
          <meshStandardMaterial color={FIT.skirting} roughness={1} />
        </mesh>
      ))}
    </>
  );
}

/** A steel door in its frame: leaf, vision panel, push bar, the exit sign over
 *  it. Closed it stands in the wall; open it is swung into the room. A watched
 *  door wears its contact on the lintel, coloured by what the contact reads. */
function Door({ len, door }: { len: number; door: RoomDoor }) {
  const open = door.open === true;
  const swing = open ? (-78 * Math.PI) / 180 : 0;
  const lamp = door.alarm ?? (open ? '#e8a020' : '#2fae6a');
  return (
    <group raycast={() => null}>
      {/* frame: two jambs and the lintel */}
      {[-1, 1].map((s) => (
        <mesh key={s} position={[s * (len / 2 + 0.04), DOOR_H / 2, 0.05]}>
          <boxGeometry args={[0.08, DOOR_H, 0.14]} /><meshStandardMaterial color={FIT.frame} roughness={0.8} />
        </mesh>
      ))}
      <mesh position={[0, DOOR_H + 0.04, 0.05]}>
        <boxGeometry args={[len + 0.16, 0.08, 0.14]} /><meshStandardMaterial color={FIT.frame} roughness={0.8} />
      </mesh>
      {/* the opening behind the leaf, dark, so an open door shows a way out */}
      <mesh position={[0, DOOR_H / 2, 0.012]}>
        <planeGeometry args={[len, DOOR_H]} /><meshBasicMaterial color="#2a2f36" />
      </mesh>
      {/* leaf, hinged on the -x jamb */}
      <group position={[-len / 2, 0, 0.06]} rotation-y={swing}>
        <mesh position={[len / 2, DOOR_H / 2, 0]}>
          <boxGeometry args={[len - 0.02, DOOR_H - 0.02, 0.05]} /><meshStandardMaterial color={FIT.leaf} roughness={0.7} />
        </mesh>
        <mesh position={[len * 0.72, 1.5, 0.028]}>
          <planeGeometry args={[len * 0.22, 0.5]} /><meshStandardMaterial color={FIT.glass} roughness={0.2} transparent opacity={0.75} />
        </mesh>
        <mesh position={[len / 2, 1.0, 0.045]}>
          <boxGeometry args={[len * 0.8, 0.05, 0.04]} /><meshStandardMaterial color={FIT.bar} roughness={0.4} metalness={0.3} />
        </mesh>
      </group>
      {/* running-man exit sign over the door */}
      <mesh position={[0, DOOR_H + 0.26, 0.04]}>
        <boxGeometry args={[0.5, 0.2, 0.05]} /><meshStandardMaterial color={FIT.exit} emissive={FIT.exit} emissiveIntensity={0.35} />
      </mesh>
      {door.monitored && (
        <mesh position={[len / 2 - 0.08, DOOR_H - 0.06, 0.13]}>
          <boxGeometry args={[0.12, 0.06, 0.04]} /><meshStandardMaterial color={lamp} emissive={lamp} emissiveIntensity={0.6} />
        </mesh>
      )}
    </group>
  );
}

/** What stands by an exit: a handheld extinguisher on its bracket and a manual
 *  call point above it; at the door of a gas-suppressed room, the release
 *  (yellow) and abort (blue) stations too. */
function FirePoint({ x, gas, counting }: { x: number; gas: boolean; counting?: boolean }) {
  return (
    <group position={[x, 0, 0.1]} raycast={() => null}>
      {/* backboard */}
      <mesh position={[0, 1.05, -0.085]}>
        <planeGeometry args={[0.34, 0.9]} /><meshStandardMaterial color="#f1f2f4" roughness={1} />
      </mesh>
      <mesh position={[0, 0.82, 0]}>
        <cylinderGeometry args={[0.075, 0.075, 0.5, 16]} /><meshStandardMaterial color={FIT.fire} roughness={0.45} />
      </mesh>
      <mesh position={[0, 1.11, 0]}>
        <cylinderGeometry args={[0.03, 0.05, 0.08, 12]} /><meshStandardMaterial color={FIT.hose} roughness={0.6} />
      </mesh>
      <mesh position={[0.07, 1.0, 0.03]} rotation-z={-0.5}>
        <cylinderGeometry args={[0.018, 0.03, 0.3, 8]} /><meshStandardMaterial color={FIT.hose} roughness={0.6} />
      </mesh>
      {/* manual call point */}
      <mesh position={[0, 1.42, -0.05]}>
        <boxGeometry args={[0.1, 0.1, 0.05]} /><meshStandardMaterial color={FIT.fire} roughness={0.6} />
      </mesh>
      {gas && (
        <>
          <mesh position={[0.3, 1.42, -0.05]}>
            <boxGeometry args={[0.11, 0.11, 0.05]} />
            <meshStandardMaterial color={FIT.release} roughness={0.6} emissive={FIT.release} emissiveIntensity={counting ? 0.8 : 0} />
          </mesh>
          <mesh position={[0.3, 1.24, -0.05]}>
            <boxGeometry args={[0.11, 0.11, 0.05]} /><meshStandardMaterial color={FIT.abort} roughness={0.6} />
          </mesh>
        </>
      )}
    </group>
  );
}

/** Three clean-agent cylinders on a rack, valves joined by a manifold that
 *  turns up the wall toward the discharge pipework. */
function CylinderBank({ spent }: { spent: boolean }) {
  const xs = [-0.5, 0, 0.5];
  // A discharged bank is drawn drained of its colour: it protects nothing until it is replaced.
  const body = spent ? '#8a8f96' : FIT.fire;
  return (
    <group position={[0, 0, 0.3]} raycast={() => null}>
      {xs.map((x) => (
        <group key={x} position={[x, 0, 0]}>
          <mesh position={[0, 0.8, 0]}>
            <cylinderGeometry args={[0.2, 0.2, 1.6, 20]} /><meshStandardMaterial color={body} roughness={0.4} />
          </mesh>
          <mesh position={[0, 1.66, 0]}>
            <sphereGeometry args={[0.2, 20, 10, 0, Math.PI * 2, 0, Math.PI / 2]} /><meshStandardMaterial color={body} roughness={0.4} />
          </mesh>
          <mesh position={[0, 1.9, 0]}>
            <cylinderGeometry args={[0.05, 0.07, 0.16, 10]} /><meshStandardMaterial color={FIT.steel} roughness={0.4} metalness={0.4} />
          </mesh>
        </group>
      ))}
      {/* retaining strap and manifold */}
      <mesh position={[0, 1.1, 0.02]}>
        <boxGeometry args={[1.5, 0.05, 0.46]} /><meshStandardMaterial color={FIT.frame} roughness={0.8} transparent opacity={0.9} />
      </mesh>
      <mesh position={[0, 2.05, -0.12]} rotation-z={Math.PI / 2}>
        <cylinderGeometry args={[0.04, 0.04, 1.3, 10]} /><meshStandardMaterial color={FIT.fireDark} roughness={0.5} />
      </mesh>
      <mesh position={[0.65, 2.45, -0.12]}>
        <cylinderGeometry args={[0.04, 0.04, 0.8, 10]} /><meshStandardMaterial color={FIT.fireDark} roughness={0.5} />
      </mesh>
    </group>
  );
}

// ------------------------------------------------------------ furniture

const FURN = {
  top: '#c9ced4', frame: '#3a4048', seat: '#2b3036', screen: '#0e141b', glow: '#2a6fb0',
  bench: '#8f979f', mat: '#5d7468', shelf: '#9aa3ad', box: '#b08a5a', board: '#f4f6f8', boardRim: '#9aa3ad',
};

/** A monitor on a short stand, its face toward +z (the person using it). */
function Monitor({ x = 0, z = 0, y = 0.74, w = 0.55 }: { x?: number; z?: number; y?: number; w?: number }) {
  return (
    <group position={[x, y, z]}>
      <mesh position={[0, 0.3, 0]}><boxGeometry args={[w, w * 0.6, 0.03]} /><meshStandardMaterial color={FURN.frame} roughness={0.6} /></mesh>
      <mesh position={[0, 0.3, 0.017]}><planeGeometry args={[w * 0.93, w * 0.54]} />
        <meshStandardMaterial color={FURN.screen} emissive={FURN.glow} emissiveIntensity={0.35} roughness={0.3} /></mesh>
      <mesh position={[0, 0.07, -0.02]}><boxGeometry args={[0.05, 0.14, 0.05]} /><meshStandardMaterial color={FURN.frame} /></mesh>
      <mesh position={[0, 0.005, -0.02]}><boxGeometry args={[0.22, 0.01, 0.16]} /><meshStandardMaterial color={FURN.frame} /></mesh>
    </group>
  );
}

/** An operator's console: a desk with its screens along the back edge. */
function Console({ len, screens }: { len: number; screens: number }) {
  const step = Math.min(0.58, (len - 0.1) / screens);
  return (
    <group raycast={() => null}>
      <mesh position={[0, 0.73, 0]}><boxGeometry args={[len, 0.04, 0.8]} /><meshStandardMaterial color={FURN.top} roughness={0.7} /></mesh>
      {[-1, 1].map((k) => (
        <mesh key={k} position={[k * (len / 2 - 0.03), 0.36, 0]}><boxGeometry args={[0.04, 0.72, 0.76]} />
          <meshStandardMaterial color={FURN.frame} roughness={0.8} /></mesh>
      ))}
      <mesh position={[0, 0.45, -0.36]}><boxGeometry args={[len - 0.08, 0.5, 0.02]} /><meshStandardMaterial color={FURN.frame} roughness={0.8} /></mesh>
      {Array.from({ length: screens }, (_, i) => (
        <Monitor key={i} x={(i - (screens - 1) / 2) * step} z={-0.22} w={step - 0.03} />
      ))}
    </group>
  );
}

/** A workbench: steel frame, an ESD mat on the top, a lower shelf, its screens at the back. */
function Bench({ len, screens }: { len: number; screens: number }) {
  return (
    <group raycast={() => null}>
      <mesh position={[0, 0.88, 0]}><boxGeometry args={[len, 0.04, 0.7]} /><meshStandardMaterial color={FURN.bench} roughness={0.6} /></mesh>
      <mesh position={[0, 0.905, 0.05]}><boxGeometry args={[len - 0.1, 0.005, 0.55]} /><meshStandardMaterial color={FURN.mat} roughness={0.9} /></mesh>
      <mesh position={[0, 0.2, 0]}><boxGeometry args={[len - 0.06, 0.03, 0.62]} /><meshStandardMaterial color={FURN.bench} roughness={0.7} /></mesh>
      {[-1, 1].map((kx) => [-1, 1].map((kz) => (
        <mesh key={`${kx}${kz}`} position={[kx * (len / 2 - 0.04), 0.44, kz * 0.31]}><boxGeometry args={[0.04, 0.88, 0.04]} />
          <meshStandardMaterial color={FURN.frame} /></mesh>
      )))}
      {Array.from({ length: screens }, (_, i) => (
        <Monitor key={i} x={(i - (screens - 1) / 2) * 0.62} z={-0.2} y={0.9} />
      ))}
    </group>
  );
}

/** A task chair: five-star base, gas lift, seat and back; it faces -z (the desk). */
function Chair({ x = 0, z = 0 }: { x?: number; z?: number }) {
  return (
    <group position={[x, 0, z]} raycast={() => null}>
      <mesh position={[0, 0.03, 0]}><cylinderGeometry args={[0.3, 0.3, 0.04, 5]} /><meshStandardMaterial color={FURN.seat} roughness={0.7} /></mesh>
      <mesh position={[0, 0.25, 0]}><cylinderGeometry args={[0.025, 0.025, 0.4, 8]} /><meshStandardMaterial color={FURN.frame} /></mesh>
      <mesh position={[0, 0.48, 0]}><boxGeometry args={[0.48, 0.07, 0.46]} /><meshStandardMaterial color={FURN.seat} roughness={0.9} /></mesh>
      <mesh position={[0, 0.82, 0.22]} rotation-x={-0.12}><boxGeometry args={[0.44, 0.52, 0.05]} /><meshStandardMaterial color={FURN.seat} roughness={0.9} /></mesh>
    </group>
  );
}

/** A lab stool at bench height. */
function Stool({ x = 0, z = 0 }: { x?: number; z?: number }) {
  return (
    <group position={[x, 0, z]} raycast={() => null}>
      <mesh position={[0, 0.02, 0]}><cylinderGeometry args={[0.24, 0.24, 0.03, 5]} /><meshStandardMaterial color={FURN.seat} /></mesh>
      <mesh position={[0, 0.33, 0]}><cylinderGeometry args={[0.025, 0.025, 0.6, 8]} /><meshStandardMaterial color={FURN.frame} /></mesh>
      <mesh position={[0, 0.66, 0]}><cylinderGeometry args={[0.19, 0.19, 0.06, 16]} /><meshStandardMaterial color={FURN.seat} roughness={0.9} /></mesh>
    </group>
  );
}

/** A whiteboard on the wall, with its pen tray. */
function Whiteboard({ len }: { len: number }) {
  return (
    <group position={[0, 1.45, 0.02]} raycast={() => null}>
      <mesh><boxGeometry args={[len + 0.06, 1.16, 0.02]} /><meshStandardMaterial color={FURN.boardRim} roughness={0.6} /></mesh>
      {/* unlit, like the walls it hangs on, so white reads as white */}
      <mesh position={[0, 0, 0.012]}><planeGeometry args={[len, 1.1]} /><meshBasicMaterial color={FURN.board} /></mesh>
      <mesh position={[0, -0.6, 0.05]}><boxGeometry args={[len * 0.6, 0.03, 0.08]} /><meshStandardMaterial color={FURN.boardRim} /></mesh>
    </group>
  );
}

/** A NOC video wall: a grid of thin-bezel panels, lit. */
function VideoWall({ w, cols = 3, rows = 2 }: { w: number; cols?: number; rows?: number }) {
  const pw = w / cols, ph = pw * 0.5625;
  return (
    <group position={[0, 1.15 + (rows * ph) / 2, 0.04]} raycast={() => null}>
      <mesh><boxGeometry args={[w + 0.06, rows * ph + 0.06, 0.06]} /><meshStandardMaterial color={FURN.frame} roughness={0.6} /></mesh>
      {Array.from({ length: cols * rows }, (_, i) => {
        const c = i % cols, r = Math.floor(i / cols);
        return (
          <mesh key={i} position={[(c - (cols - 1) / 2) * pw, (r - (rows - 1) / 2) * ph, 0.032]}>
            <planeGeometry args={[pw - 0.015, ph - 0.015]} />
            <meshStandardMaterial color={FURN.screen} emissive={FURN.glow} emissiveIntensity={0.45 + 0.15 * ((i * 7) % 3) / 2} roughness={0.3} />
          </mesh>
        );
      })}
    </group>
  );
}

/** Steel shelving bays against the wall, stocked with boxed spares. */
function Shelving({ len }: { len: number }) {
  const bays = Math.max(1, Math.round(len / 0.9)), bw = len / bays, deepS = 0.5;
  const boxes: [number, number, number, number][] = [];
  for (let b = 0; b < bays; b++) {
    for (const [k, y] of [0.17, 0.62, 1.07, 1.52].entries()) {
      const n = 1 + ((b * 3 + k * 5) % 3);
      for (let i = 0; i < n; i++) {
        const w = bw / (n + 0.6), h = 0.18 + (((b + i + k) * 13) % 5) * 0.04;
        boxes.push([-len / 2 + b * bw + (i + 0.5) * (bw / n), y + h / 2 + 0.02, w, h]);
      }
    }
  }
  return (
    <group position={[0, 0, 0.05 + deepS / 2]} raycast={() => null}>
      {Array.from({ length: bays + 1 }, (_, i) => [-1, 1].map((k) => (
        <mesh key={`${i}${k}`} position={[-len / 2 + i * bw, 0.95, k * (deepS / 2 - 0.02)]}><boxGeometry args={[0.035, 1.9, 0.035]} />
          <meshStandardMaterial color={FURN.frame} /></mesh>
      )))}
      {[0.15, 0.6, 1.05, 1.5, 1.95].map((y) => (
        <mesh key={y} position={[0, y, 0]}><boxGeometry args={[len, 0.025, deepS]} /><meshStandardMaterial color={FURN.shelf} roughness={0.6} /></mesh>
      ))}
      {boxes.map(([x, y, w, h], i) => (
        <mesh key={i} position={[x, y, 0]}><boxGeometry args={[w, h, deepS * 0.8]} /><meshStandardMaterial color={FURN.box} roughness={0.9} /></mesh>
      ))}
    </group>
  );
}

/** A yard or a roof has no walls: a compound fence, or a parapet. */
export function OpenEnclosure({ W, D, kind }: { W: number; D: number; kind: 'fence' | 'parapet' }) {
  const geom = useMemo(() => {
    const pts: number[] = [];
    if (kind === 'fence') {
      const H = 2.4, step = 2.5;
      const edge = (ax: number, az: number, bx: number, bz: number) => {
        const n = Math.max(1, Math.round(Math.hypot(bx - ax, bz - az) / step));
        for (let i = 0; i <= n; i++) {
          const x = ax + ((bx - ax) * i) / n, z = az + ((bz - az) * i) / n;
          pts.push(x, 0, z, x, H, z);
        }
        for (const y of [0.05, H * 0.5, H]) pts.push(ax, y, az, bx, y, bz);
      };
      edge(-W / 2, -D / 2, W / 2, -D / 2); edge(W / 2, -D / 2, W / 2, D / 2);
      edge(W / 2, D / 2, -W / 2, D / 2); edge(-W / 2, D / 2, -W / 2, -D / 2);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
    return g;
  }, [W, D, kind]);
  useEffect(() => () => geom.dispose(), [geom]);
  if (kind === 'fence') {
    return <lineSegments geometry={geom} raycast={() => null}><lineBasicMaterial color={FIT.fence} transparent opacity={0.8} /></lineSegments>;
  }
  const sides: [number, number, number, number][] = [
    [0, -D / 2, W, 0.2], [0, D / 2, W, 0.2], [-W / 2, 0, 0.2, D], [W / 2, 0, 0.2, D],
  ];
  return (
    <group raycast={() => null}>
      {sides.map(([x, z, w, d], i) => (
        <mesh key={i} position={[x, 0.55, z]}>
          <boxGeometry args={[w, 1.1, d]} /><meshStandardMaterial color="#cfd3d9" roughness={1} />
        </mesh>
      ))}
    </group>
  );
}

// ------------------------------------------------------------ the cabinets

/** A perforated cabinet door: a frame, a handle, and holes the layer's colour
 *  shows through. `split` draws the rear's pair of half doors. */
function doorTexture(split: boolean): THREE.CanvasTexture {
  return canvasTex(128, 448, (c, w, h) => {
    c.clearRect(0, 0, w, h);
    c.fillStyle = `rgba(${INK},0.2)`;
    for (let y = 12; y < h - 10; y += 7) {
      for (let x = 12 + ((y / 7) % 2) * 3.5; x < w - 10; x += 7) c.fillRect(x, y, 3, 3);
    }
    c.strokeStyle = `rgba(${INK},0.78)`; c.lineWidth = 9; c.strokeRect(4.5, 4.5, w - 9, h - 9);
    if (split) {
      c.lineWidth = 5; c.beginPath(); c.moveTo(w / 2, 6); c.lineTo(w / 2, h - 6); c.stroke();
    } else {
      c.fillStyle = `rgba(${INK},0.85)`; c.fillRect(w - 26, h / 2 - 34, 9, 68);
      c.fillStyle = 'rgba(235,238,242,0.9)'; c.fillRect(w - 24, h / 2 - 6, 5, 12);
    }
  });
}

/** A cabinet's roof: the frame, and the brush-strip cable entries at the rear. */
function roofTexture(): THREE.CanvasTexture {
  return canvasTex(128, 256, (c, w, h) => {
    c.clearRect(0, 0, w, h);
    c.strokeStyle = `rgba(${INK},0.6)`; c.lineWidth = 8; c.strokeRect(4, 4, w - 8, h - 8);
    c.fillStyle = `rgba(${INK},0.5)`;
    c.fillRect(22, 20, w - 44, 22); c.fillRect(22, 52, w - 44, 12);
    c.strokeStyle = `rgba(${INK},0.22)`; c.lineWidth = 2;
    for (let y = 96; y < h - 20; y += 12) { c.beginPath(); c.moveTo(22, y); c.lineTo(w - 22, y); c.stroke(); }
  });
}

/** Doors, roofs and plinths for every cabinet in one instanced pass each.
 *  `open` leaves the front door off, for "See into racks". */
export function RackDetails({ boxes, open }: { boxes: GBox[]; open: boolean }) {
  const front = useRef<THREE.InstancedMesh>(null), rear = useRef<THREE.InstancedMesh>(null);
  const roof = useRef<THREE.InstancedMesh>(null), plinth = useRef<THREE.InstancedMesh>(null);
  const n = boxes.length;
  const tex = useMemo(() => ({ front: doorTexture(false), rear: doorTexture(true), roof: roofTexture() }), []);
  useEffect(() => () => { tex.front.dispose(); tex.rear.dispose(); tex.roof.dispose(); }, [tex]);

  useLayoutEffect(() => {
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), p = new THREE.Vector3(), s = new THREE.Vector3();
    const up = new THREE.Vector3(0, 1, 0), flat = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), -Math.PI / 2);
    const hide = new THREE.Matrix4().makeScale(0, 0, 0);
    boxes.forEach((b, i) => {
      const y0 = b.y - b.h / 2;
      // A cabinet whose front is unknown gets no doors: there is no face to hang them on.
      for (const [ref, sign] of [[front, b.front], [rear, -b.front]] as const) {
        const im = ref.current;
        if (!im) continue;
        if (!sign) { im.setMatrixAt(i, hide); continue; }
        q.setFromAxisAngle(up, sign > 0 ? 0 : Math.PI);
        im.setMatrixAt(i, m.compose(p.set(b.x, b.y, b.z + sign * (b.d / 2 + 0.006)), q, s.set(b.w, b.h, 1)));
      }
      roof.current?.setMatrixAt(i, m.compose(p.set(b.x, y0 + b.h + 0.006, b.z),
        // the brush strips sit at the rear: turn the roof so its top edge is over the back door
        new THREE.Quaternion().setFromAxisAngle(up, b.front > 0 ? Math.PI : 0).multiply(flat), s.set(b.w, b.d, 1)));
      plinth.current?.setMatrixAt(i, m.compose(p.set(b.x, y0 + 0.04, b.z), q.identity(), s.set(b.w + 0.012, 0.08, b.d + 0.012)));
    });
    for (const ref of [front, rear, roof, plinth]) {
      if (ref.current) { ref.current.instanceMatrix.needsUpdate = true; ref.current.computeBoundingSphere(); }
    }
  }, [boxes, open]);

  if (!n) return null;
  const skin = (map: THREE.Texture) => (
    <meshBasicMaterial map={map} transparent depthWrite={false} polygonOffset polygonOffsetFactor={-2} />
  );
  return (
    <group>
      {!open && (
        <instancedMesh key={`f${n}`} ref={front} args={[undefined, undefined, n]} frustumCulled={false} raycast={() => null}>
          <planeGeometry args={[1, 1]} />{skin(tex.front)}
        </instancedMesh>
      )}
      <instancedMesh key={`r${n}`} ref={rear} args={[undefined, undefined, n]} frustumCulled={false} raycast={() => null}>
        <planeGeometry args={[1, 1]} />{skin(tex.rear)}
      </instancedMesh>
      <instancedMesh key={`t${n}`} ref={roof} args={[undefined, undefined, n]} frustumCulled={false} raycast={() => null}>
        <planeGeometry args={[1, 1]} />{skin(tex.roof)}
      </instancedMesh>
      <instancedMesh key={`p${n}`} ref={plinth} args={[undefined, undefined, n]} frustumCulled={false} raycast={() => null}>
        <boxGeometry args={[1, 1, 1]} /><meshStandardMaterial color={FIT.plinth} roughness={0.9} />
      </instancedMesh>
    </group>
  );
}

// ---------------------------------------------------------------- the plant

type UnitKind = 'crah' | 'cabinet' | 'fans' | 'chiller' | 'generator' | 'transformer' | 'pump' | 'none';
function kindOf(deviceType: string): UnitKind {
  switch (deviceType) {
    case 'crah': case 'crac': case 'in_row_cooler': return 'crah';
    case 'ups': case 'rpp': case 'mpp': case 'ats': case 'sts': case 'switchgear': case 'mcc':
    case 'floor_pdu': case 'pdu': case 'battery': case 'cdu': return 'cabinet';
    // A chiller in a plant room with towers on the roof is water-cooled: two
    // shell-and-tube barrels and a starter panel, no condenser fans.
    case 'chiller': return 'chiller';
    case 'cooling_tower': case 'dry_cooler': case 'condenser': return 'fans';
    case 'generator': return 'generator';
    case 'transformer': return 'transformer';
    case 'pump': return 'pump';
    default: return 'none';
  }
}

/** One door section of a floor cabinet: seam, handle, a meter window, and the
 *  vent louvres at its foot. Repeated along the face, one per section. */
function sectionTexture(): THREE.CanvasTexture {
  const t = canvasTex(128, 320, (c, w, h) => {
    c.clearRect(0, 0, w, h);
    c.strokeStyle = `rgba(${INK},0.6)`; c.lineWidth = 5; c.strokeRect(2.5, 2.5, w - 5, h - 5);
    c.fillStyle = `rgba(${INK},0.8)`; c.fillRect(w - 20, h * 0.44, 7, 44);
    c.fillStyle = 'rgba(16,20,26,0.82)'; c.fillRect(26, 34, w - 64, 40);
    c.fillStyle = 'rgba(120,220,170,0.9)'; c.fillRect(32, 42, 30, 6);
    c.strokeStyle = `rgba(${INK},0.4)`; c.lineWidth = 3;
    for (let y = h - 62; y < h - 14; y += 9) { c.beginPath(); c.moveTo(18, y); c.lineTo(w - 18, y); c.stroke(); }
  });
  t.wrapS = THREE.RepeatWrapping;
  return t;
}

/** An air handler's face: service doors over a full-width louvre. */
function crahTexture(): THREE.CanvasTexture {
  return canvasTex(256, 288, (c, w, h) => {
    c.clearRect(0, 0, w, h);
    c.strokeStyle = `rgba(${INK},0.6)`; c.lineWidth = 5; c.strokeRect(2.5, 2.5, w - 5, h - 5);
    for (const x of [w / 3, (2 * w) / 3]) { c.beginPath(); c.moveTo(x, 4); c.lineTo(x, h * 0.72); c.stroke(); }
    c.beginPath(); c.moveTo(4, h * 0.72); c.lineTo(w - 4, h * 0.72); c.stroke();
    c.fillStyle = 'rgba(16,20,26,0.85)'; c.fillRect(w / 3 + 22, 26, w / 3 - 44, 44);
    c.fillStyle = 'rgba(120,200,235,0.92)'; c.fillRect(w / 3 + 30, 36, 40, 7);
    c.strokeStyle = `rgba(${INK},0.45)`; c.lineWidth = 3;
    for (let y = h * 0.72 + 10; y < h - 8; y += 8) { c.beginPath(); c.moveTo(12, y); c.lineTo(w - 12, y); c.stroke(); }
  });
}

/** A grille: the return-air filter bank on top of an air handler. */
function grilleTexture(): THREE.CanvasTexture {
  return canvasTex(256, 128, (c, w, h) => {
    c.clearRect(0, 0, w, h);
    c.fillStyle = `rgba(${INK},0.3)`; c.fillRect(10, 10, w - 20, h - 20);
    c.strokeStyle = `rgba(${INK},0.55)`; c.lineWidth = 2;
    for (let x = 10; x <= w - 10; x += 12) { c.beginPath(); c.moveTo(x, 10); c.lineTo(x, h - 10); c.stroke(); }
    c.lineWidth = 5; c.strokeRect(10, 10, w - 20, h - 20);
    for (const x of [w / 3, (2 * w) / 3]) { c.beginPath(); c.moveTo(x, 10); c.lineTo(x, h - 10); c.stroke(); }
  });
}

/** One fan in its shroud, seen from above; repeated along the unit. */
function fanTexture(): THREE.CanvasTexture {
  const t = canvasTex(160, 160, (c, w, h) => {
    c.clearRect(0, 0, w, h);
    const r = w * 0.4;
    c.fillStyle = `rgba(${INK},0.5)`; c.beginPath(); c.arc(w / 2, h / 2, r, 0, Math.PI * 2); c.fill();
    c.strokeStyle = `rgba(${INK},0.8)`; c.lineWidth = 6; c.beginPath(); c.arc(w / 2, h / 2, r, 0, Math.PI * 2); c.stroke();
    c.strokeStyle = 'rgba(225,230,236,0.55)'; c.lineWidth = 9; c.lineCap = 'round';
    for (let k = 0; k < 5; k++) {
      const a = (k / 5) * Math.PI * 2;
      c.beginPath(); c.moveTo(w / 2, h / 2); c.lineTo(w / 2 + Math.cos(a) * r * 0.82, h / 2 + Math.sin(a) * r * 0.82); c.stroke();
    }
    c.fillStyle = `rgba(${INK},0.9)`; c.beginPath(); c.arc(w / 2, h / 2, r * 0.2, 0, Math.PI * 2); c.fill();
  });
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  return t;
}

/** Louvres across a whole face: an engine enclosure, a radiator bank. */
function louvreTexture(): THREE.CanvasTexture {
  const t = canvasTex(64, 64, (c, w, h) => {
    c.clearRect(0, 0, w, h);
    c.strokeStyle = `rgba(${INK},0.42)`; c.lineWidth = 3;
    for (let y = 4; y < h; y += 8) { c.beginPath(); c.moveTo(0, y); c.lineTo(w, y); c.stroke(); }
  });
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  return t;
}

/** Faces, grilles and the few solid parts that make floor-standing plant
 *  recognisable. The body under them is still the layer's colour. */
export function UnitDetails({ boxes, types }: { boxes: GBox[]; types: string[] }) {
  const base = useMemo(() => ({
    section: sectionTexture(), crah: crahTexture(), grille: grilleTexture(), fan: fanTexture(), louvre: louvreTexture(),
  }), []);
  // Repeating textures are cloned per unit so each can carry its own repeat.
  const skins = useMemo(() => boxes.map((b, i) => {
    const kind = kindOf(types[i] ?? '');
    const rep = (t: THREE.CanvasTexture, x: number, y = 1) => { const k = t.clone(); k.repeat.set(x, y); k.needsUpdate = true; return k; };
    const long = Math.max(b.w, b.d), short = Math.min(b.w, b.d);
    return {
      kind,
      section: kind === 'cabinet' ? rep(base.section, Math.max(1, Math.round(b.w / 0.8)))
        : kind === 'chiller' ? rep(base.section, 1) : null,
      fans: kind === 'fans' ? rep(base.fan, b.w >= b.d ? Math.max(1, Math.round(long / short)) : 1,
                                  b.w >= b.d ? 1 : Math.max(1, Math.round(long / short))) : null,
      louvre: kind === 'fans' || kind === 'generator' || kind === 'transformer'
        ? rep(base.louvre, Math.max(1, Math.round(b.d / 0.5)), Math.max(1, Math.round(b.h / 0.5))) : null,
    };
  }), [boxes, types, base]);
  useEffect(() => () => { for (const s of skins) { s.section?.dispose(); s.fans?.dispose(); s.louvre?.dispose(); } }, [skins]);
  useEffect(() => () => { Object.values(base).forEach((t) => t.dispose()); }, [base]);

  const skin = (map: THREE.Texture) => (
    <meshBasicMaterial map={map} transparent depthWrite={false} polygonOffset polygonOffsetFactor={-2} />
  );
  return (
    <group>
      {boxes.map((b, i) => {
        const s = skins[i];
        if (s.kind === 'none') return null;
        const face = (map: THREE.Texture) => (
          <mesh position={[0, 0, -(b.d / 2 + 0.006)]} rotation-y={Math.PI} raycast={() => null}>
            <planeGeometry args={[b.w, b.h]} />{skin(map)}
          </mesh>
        );
        const top = (map: THREE.Texture, inset = 0) => (
          <mesh position={[0, b.h / 2 + 0.006, 0]} rotation-x={-Math.PI / 2} raycast={() => null}>
            <planeGeometry args={[b.w - inset, b.d - inset]} />{skin(map)}
          </mesh>
        );
        const sides = (map: THREE.Texture) => [-1, 1].map((sx) => (
          <mesh key={sx} position={[sx * (b.w / 2 + 0.006), 0, 0]} rotation-y={(sx * Math.PI) / 2} raycast={() => null}>
            <planeGeometry args={[b.d, b.h]} />{skin(map)}
          </mesh>
        ));
        return (
          <group key={i} position={[b.x, b.y, b.z]} rotation-y={b.rot}>
            {s.kind === 'crah' && <>{face(base.crah)}{top(base.grille, 0.1)}</>}
            {s.kind === 'cabinet' && s.section && face(s.section)}
            {s.kind === 'fans' && s.fans && <>{top(s.fans, 0.15)}{s.louvre && sides(s.louvre)}</>}
            {s.kind === 'chiller' && (() => {
              // Evaporator and condenser shells, lying along the unit's long side.
              const alongZ = b.d >= b.w, long = Math.max(b.w, b.d), short = Math.min(b.w, b.d);
              const r = Math.min(short * 0.2, 0.42);
              return (
                <>
                  {[-1, 1].map((k) => (
                    <mesh key={k} raycast={() => null}
                          position={alongZ ? [k * short * 0.24, b.h / 2 + r * 0.55, 0] : [0, b.h / 2 + r * 0.55, k * short * 0.24]}
                          rotation-x={alongZ ? Math.PI / 2 : 0} rotation-z={alongZ ? 0 : Math.PI / 2}>
                      <cylinderGeometry args={[r, r, long * 0.86, 18]} /><meshStandardMaterial color={FIT.stack} roughness={0.55} />
                    </mesh>
                  ))}
                  {/* the starter and control panel on the end the unit is served from */}
                  {s.section && (
                    <mesh position={[0, 0, -(b.d / 2 + 0.006)]} rotation-y={Math.PI} raycast={() => null}>
                      <planeGeometry args={[Math.min(b.w, 1.0), Math.min(b.h, 1.9)]} />{skin(s.section)}
                    </mesh>
                  )}
                </>
              );
            })()}
            {s.kind === 'generator' && (
              <>
                {s.louvre && sides(s.louvre)}
                {/* exhaust stack and silencer on the roof of the enclosure */}
                <mesh position={[b.w * 0.2, b.h / 2 + 0.45, b.d * 0.3]} raycast={() => null}>
                  <cylinderGeometry args={[0.16, 0.16, 0.9, 14]} /><meshStandardMaterial color={FIT.stack} roughness={0.6} />
                </mesh>
                <mesh position={[0, b.h / 2 + 0.2, -b.d * 0.15]} rotation-x={Math.PI / 2} raycast={() => null}>
                  <cylinderGeometry args={[0.24, 0.24, Math.min(2.2, b.d * 0.4), 14]} /><meshStandardMaterial color={FIT.stack} roughness={0.6} />
                </mesh>
              </>
            )}
            {s.kind === 'transformer' && (
              <>
                {s.louvre && sides(s.louvre)}
                {[-1, 0, 1].map((k) => (
                  <mesh key={k} position={[k * b.w * 0.25, b.h / 2 + 0.22, 0]} raycast={() => null}>
                    <cylinderGeometry args={[0.05, 0.09, 0.44, 10]} /><meshStandardMaterial color="#b9794a" roughness={0.5} />
                  </mesh>
                ))}
                <mesh position={[b.w * 0.3, b.h / 2 + 0.16, b.d * 0.3]} rotation-z={Math.PI / 2} raycast={() => null}>
                  <cylinderGeometry args={[0.16, 0.16, b.w * 0.5, 12]} /><meshStandardMaterial color={FIT.stack} roughness={0.6} />
                </mesh>
              </>
            )}
            {s.kind === 'pump' && (
              <mesh position={[0, b.h / 2 + 0.16, 0]} raycast={() => null}>
                <cylinderGeometry args={[Math.min(b.w, b.d) * 0.32, Math.min(b.w, b.d) * 0.32, 0.32, 14]} />
                <meshStandardMaterial color={FIT.stack} roughness={0.6} />
              </mesh>
            )}
          </group>
        );
      })}
    </group>
  );
}
