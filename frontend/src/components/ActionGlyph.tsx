/** The glyphs that sit before an action's word.
 *
 *  Never alone: a pencil reads as "edit" on its own, a cube does not read as
 *  "the room in 3D" to anyone who has not been told, so every one of these
 *  is drawn beside its label and hidden from the accessibility tree. Same
 *  16-unit grid and line weight as the viewer's tile glyphs (features/
 *  floorplan/glyphs.tsx), so a button in the page head and a tile in the
 *  panel are visibly one family. */

export type ActionGlyphKind = 'edit' | 'cube' | 'tag' | 'open';

export function ActionGlyph({ kind, size = 16 }: { kind: ActionGlyphKind; size?: number }) {
  const common = {
    width: size, height: size, viewBox: '0 0 16 16',
    fill: 'none', stroke: 'currentColor', strokeWidth: 1.4,
    strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
    'aria-hidden': true, focusable: false as const, className: 'act-glyph',
  };
  switch (kind) {
    // A pencil, tip down-left, the way one is held to a form.
    case 'edit':
      return (
        <svg {...common}>
          <path d="M11.2 2.6l2.2 2.2L5.6 12.6 2.6 13.4l.8-3L11.2 2.6z" />
          <path d="M9.8 4l2.2 2.2" />
        </svg>
      );
    // A cube in isometric view: the room, seen as a volume.
    case 'cube':
      return (
        <svg {...common}>
          <path d="M8 1.8l5.4 3.1v6.2L8 14.2l-5.4-3.1V4.9L8 1.8z" />
          <path d="M2.6 4.9L8 8l5.4-3.1M8 8v6.2" />
        </svg>
      );
    // A luggage tag with its hole.
    case 'tag':
      return (
        <svg {...common}>
          <path d="M2.4 2.4h5.3l6 6-5.3 5.3-6-6V2.4z" />
          <circle cx="5.4" cy="5.4" r="1" />
        </svg>
      );
    // A window with the corner lifted: out to another page.
    case 'open':
      return (
        <svg {...common}>
          <path d="M7 3H3v10h10V9" />
          <path d="M9.5 2.5H13.5V6.5M13.5 2.5L7.5 8.5" />
        </svg>
      );
  }
}
