/**
 * What a support room is for, and where its furniture stands - the rule the
 * room view draws in detail (Fittings.tsx) and the building view draws as
 * blocks, kept in one place so the two agree.
 *
 * Nothing reports furniture: it is drawn by rule, never answers the pointer
 * and never takes a layer's colour. The far wall faces the door; the side
 * walls flank it.
 */

export type RoomKind = 'ops' | 'mmr' | 'staging';
export type Wall = 'W' | 'E' | 'N' | 'S';

export function kindOfRoom(name: string): RoomKind | null {
  if (/operat|noc|control/i.test(name)) return 'ops';
  if (/meet.?me|\bmmr\b/i.test(name)) return 'mmr';
  if (/stag|storage|store/i.test(name)) return 'staging';
  return null;
}

/** The wall a door stands in: the one its centre is nearest to (room metres). */
export function wallNearest(x: number, y: number, W: number, D: number): Wall {
  const gaps: [Wall, number][] = [['W', Math.abs(x)], ['E', Math.abs(W - x)], ['N', Math.abs(y)], ['S', Math.abs(D - y)]];
  return gaps.sort((a, b) => a[1] - b[1])[0][0];
}

/** One block of furniture, room metres: centre x/y, size along x / y, bottom and height. */
export interface Block { x: number; y: number; sx: number; sy: number; y0: number; h: number; kind: 'body' | 'screen' | 'board' }

/** The furniture of a support room as blocks, for the building view. */
export function furnitureBlocks(kind: RoomKind, W: number, D: number, doorWall: Wall): Block[] {
  const front: Wall = ({ W: 'E', E: 'W', N: 'S', S: 'N' } as const)[doorWall];
  const sides: [Wall, Wall] = front === 'N' || front === 'S' ? ['W', 'E'] : ['N', 'S'];
  const span = (w: Wall) => (w === 'N' || w === 'S' ? W : D);
  const deep = (w: Wall) => (w === 'N' || w === 'S' ? D : W);
  const out: Block[] = [];
  // A box in a wall's frame (lx along the wall from its middle, lz into the room) to room metres.
  const put = (w: Wall, lx: number, lz: number, len: number, dep: number, y0: number, h: number, k: Block['kind']) => {
    const at = span(w) / 2;
    const along = w === 'N' || w === 'S';
    const [x, y] = w === 'N' ? [at + lx, lz] : w === 'S' ? [at - lx, D - lz] : w === 'W' ? [lz, at - lx] : [W - lz, at + lx];
    out.push({ x, y, sx: along ? len : dep, sy: along ? dep : len, y0, h, kind: k });
  };
  if (kind === 'ops') {
    const vw = Math.min(3.6, span(front) * 0.62);
    put(front, 0, 0.05, vw, 0.08, 1.15, vw / 3 * 0.5625 * 2, 'screen');
    const len = Math.min(1.8, span(front) * 0.32), z = Math.min(2.2, deep(front) * 0.45);
    for (const k of [-1, 1]) {
      put(front, k * (len / 2 + 0.15), z, len, 0.8, 0, 0.75, 'body');
      put(front, k * (len / 2 + 0.15), z - 0.22, len * 0.9, 0.05, 0.75, 0.36, 'screen');
      put(front, k * (len / 2 + 0.15), z + 0.75, 0.5, 0.5, 0, 0.95, 'body');
    }
    const b = Math.min(1.8, span(sides[0]) * 0.4);
    put(sides[0], 0, 0.03, b, 0.04, 0.9, 1.1, 'board');
  } else if (kind === 'mmr') {
    const b = Math.min(1.6, span(front) * 0.45);
    put(front, 0, 0.03, b, 0.04, 0.9, 1.1, 'board');
    const len = Math.min(1.4, span(sides[0]) * 0.35);
    put(sides[0], 0, 0.4, len, 0.7, 0, 0.9, 'body');
    put(sides[0], 0, 0.2, 0.55, 0.05, 0.9, 0.36, 'screen');
    put(sides[0], 0, 1.15, 0.4, 0.4, 0, 0.7, 'body');
  } else {
    const len = Math.min(4, span(front) * 0.6);
    put(front, 0, 0.3, len, 0.5, 0, 2.0, 'body');
    const bl = Math.min(2.4, span(sides[0]) * 0.5);
    put(sides[0], 0, 0.4, bl, 0.7, 0, 0.9, 'body');
    put(sides[0], 0, 0.2, 1.2, 0.05, 0.9, 0.36, 'screen');
    for (const x of [-0.5, 0.5]) put(sides[0], x, 1.15, 0.4, 0.4, 0, 0.7, 'body');
    const b = Math.min(1.8, span(sides[1]) * 0.4);
    put(sides[1], 0, 0.03, b, 0.04, 0.9, 1.1, 'board');
  }
  return out;
}
