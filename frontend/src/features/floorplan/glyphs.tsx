/**
 * One glyph per kind of reading on the viewer's tiles.
 *
 * A tile's label says what the number is; the glyph says it before the label
 * is read, and it still says it when the panel is skimmed from across the
 * desk. Drawn here on the same 16-unit grid and line weight as the alarm
 * category glyphs (components/CategoryGlyph.tsx), for the same reasons: no
 * icon font to fail, and shape that reads in black and white.
 *
 * The shapes are the physical thing, not a dial: air streams into a chassis
 * edge for an intake and out of one for an exhaust, heat shimmers, water runs
 * along a pipe, a strip's load is a gauge. The keys are the tile labels the
 * panels already use, so a tile gets its glyph from its label and a new tile
 * with a known label gets one for free.
 */

export type GlyphName =
  | 'air-in' | 'air-out' | 'heat' | 'power' | 'humidity' | 'dew' | 'chip' | 'memory'
  | 'load' | 'current' | 'water-in' | 'water-out' | 'flow' | 'setpoint' | 'spread';

const BY_LABEL: Record<string, GlyphName> = {
  INLET: 'air-in', AIR: 'air-in', 'INTAKE MAX': 'air-in',
  EXHAUST: 'air-out', 'EXHAUST MAX': 'air-out',
  TEMP: 'heat', 'CPU TEMP': 'heat',
  POWER: 'power',
  RH: 'humidity',
  'DEW POINT': 'dew',
  CPU: 'chip',
  MEMORY: 'memory',
  LOAD: 'load',
  CURRENT: 'current',
  SUPPLY: 'water-in',
  RETURN: 'water-out',
  FLOW: 'flow',
  SETPOINT: 'setpoint',
  SPREAD: 'spread',
};

export function glyphFor(label: string): GlyphName | null {
  return BY_LABEL[label.trim().toUpperCase()] ?? null;
}

export function TileGlyph({ name, size = 16 }: { name: GlyphName; size?: number }) {
  const common = {
    width: size, height: size, viewBox: '0 0 16 16',
    fill: 'none', stroke: 'currentColor', strokeWidth: 1.4,
    strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
    'aria-hidden': true, focusable: false as const, className: 'vw-glyph',
  };
  switch (name) {
    // Air streaming into a chassis edge: the intake side.
    case 'air-in':
      return (
        <svg {...common}>
          <line x1="12.5" y1="3" x2="12.5" y2="13" strokeWidth="1.8" />
          <path d="M2 5h6.5M2 8h7.5M2 11h6.5" />
          <path d="M7 3.5 8.5 5 7 6.5M8 6.5 9.5 8 8 9.5M7 9.5 8.5 11 7 12.5" />
        </svg>
      );
    // Air leaving a chassis edge: the exhaust side.
    case 'air-out':
      return (
        <svg {...common}>
          <line x1="3.5" y1="3" x2="3.5" y2="13" strokeWidth="1.8" />
          <path d="M6 5h6.5M6 8h7.5M6 11h6.5" />
          <path d="M11 3.5 12.5 5 11 6.5M12 6.5 13.5 8 12 9.5M11 9.5 12.5 11 11 12.5" />
        </svg>
      );
    // Heat shimmer: three columns of warm air rising off a surface.
    case 'heat':
      return (
        <svg {...common}>
          <path d="M4 12.5c-1.4-1.3-1.4-2.6 0-3.9s1.4-2.6 0-3.9" />
          <path d="M8 12.5c-1.4-1.3-1.4-2.6 0-3.9s1.4-2.6 0-3.9" />
          <path d="M12 12.5c-1.4-1.3-1.4-2.6 0-3.9s1.4-2.6 0-3.9" />
          <line x1="2.5" y1="14.5" x2="13.5" y2="14.5" />
        </svg>
      );
    // A bolt: electrical draw.
    case 'power':
      return (
        <svg {...common}>
          <path d="M9 1.5 3.8 9h3.7l-.9 5.5L12.2 7H8.5z" />
        </svg>
      );
    // A drop part filled: relative humidity is how full the air is.
    case 'humidity':
      return (
        <svg {...common}>
          <path d="M8 1.8c2.3 3 4.3 5.3 4.3 8a4.3 4.3 0 0 1-8.6 0c0-2.7 2-5 4.3-8Z" />
          <path d="M4.3 9.6c1 .8 2.1.8 3.1 0s2.1-.8 3.1 0 1.2.6 1.6.2" />
        </svg>
      );
    // A drop sitting on a surface: dew, the point where it forms.
    case 'dew':
      return (
        <svg {...common}>
          <path d="M8 2.5c1.8 2.4 3.3 4.1 3.3 6.2a3.3 3.3 0 0 1-6.6 0c0-2.1 1.5-3.8 3.3-6.2Z" />
          <line x1="2" y1="14" x2="14" y2="14" />
          <line x1="4.5" y1="12" x2="4.5" y2="12.01" strokeWidth="2" />
          <line x1="11.5" y1="12" x2="11.5" y2="12.01" strokeWidth="2" />
        </svg>
      );
    // A chip with its pins: the processor.
    case 'chip':
      return (
        <svg {...common}>
          <rect x="4" y="4" width="8" height="8" rx="1.2" />
          <rect x="6.5" y="6.5" width="3" height="3" fill="currentColor" stroke="none" />
          <path d="M6 1.5v2.5M10 1.5v2.5M6 12v2.5M10 12v2.5M1.5 6h2.5M1.5 10h2.5M12 6h2.5M12 10h2.5" />
        </svg>
      );
    // A memory module: the board and its edge connector.
    case 'memory':
      return (
        <svg {...common}>
          <rect x="1.5" y="4" width="13" height="7" rx="1" />
          <path d="M4 11v2.5M6.5 11v2.5M9.5 11v2.5M12 11v2.5" />
          <path d="M4 6.5h2M7 6.5h2M10 6.5h2" />
        </svg>
      );
    // A gauge: how much of the rating is in use.
    case 'load':
      return (
        <svg {...common}>
          <path d="M2.5 12A6 6 0 0 1 13.5 12" />
          <path d="M8 12 11 7" strokeWidth="1.6" />
          <circle cx="8" cy="12" r="1.1" fill="currentColor" stroke="none" />
          <path d="M3.4 9.2l.9.4M12.6 9.2l-.9.4" />
        </svg>
      );
    // A cycle of the mains: current, in amps.
    case 'current':
      return (
        <svg {...common}>
          <path d="M1.5 8c1.6-4.5 3.2-4.5 4.8 0s3.2 4.5 4.8 0 3.2-4.5 3.4 0" />
          <line x1="1.5" y1="13" x2="14.5" y2="13" strokeDasharray="1.5 2" />
        </svg>
      );
    // Water entering the pipe: the supply side.
    case 'water-in':
      return (
        <svg {...common}>
          <path d="M7.5 5.5h6a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1h-6" />
          <path d="M1.5 8h5M4.5 6 6.5 8l-2 2" />
          <path d="M10 11.5c.7 1 1.3 1.6 1.3 2.4a1.3 1.3 0 0 1-2.6 0c0-.8.6-1.4 1.3-2.4Z" />
        </svg>
      );
    // Water leaving the pipe: the return side.
    case 'water-out':
      return (
        <svg {...common}>
          <path d="M8.5 5.5h-6a1 1 0 0 0-1 1v3a1 1 0 0 0 1 1h6" />
          <path d="M9.5 8h5M12.5 6l2 2-2 2" />
          <path d="M6 11.5c.7 1 1.3 1.6 1.3 2.4a1.3 1.3 0 0 1-2.6 0c0-.8.6-1.4 1.3-2.4Z" />
        </svg>
      );
    // Three strands running one way: flow.
    case 'flow':
      return (
        <svg {...common}>
          <path d="M2 4.5h9M2 8h11M2 11.5h9" />
          <path d="M9.5 3 11 4.5 9.5 6M11.5 6.5 13 8l-1.5 1.5M9.5 10l1.5 1.5-1.5 1.5" />
        </svg>
      );
    // A target with its mark: the commanded value.
    case 'setpoint':
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="5.5" />
          <circle cx="8" cy="8" r="1.4" fill="currentColor" stroke="none" />
          <path d="M8 1v2.5M8 12.5V15M1 8h2.5M12.5 8H15" />
        </svg>
      );
    // Two arrows parting: how far apart the readings sit.
    case 'spread':
      return (
        <svg {...common}>
          <path d="M8 2v12" />
          <path d="M5.5 4.5 8 2l2.5 2.5M5.5 11.5 8 14l2.5-2.5" />
          <path d="M3 8h2M11 8h2" />
        </svg>
      );
    default:
      return null;
  }
}
