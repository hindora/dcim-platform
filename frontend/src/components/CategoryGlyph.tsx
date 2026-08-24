/**
 * One glyph per alert category.
 *
 * These exist so the indicator cells are not distinguished by colour alone.
 * An operator with deuteranopia, and a runbook printed in black and white,
 * both still get the category from the shape.
 *
 * Drawn rather than pulled from an icon font: the home page renders one per
 * category per row, and a font that fails to load would leave a grid of
 * tofu boxes where the estate's health is supposed to be.
 */

export type GlyphKind = 'thermal' | 'connectivity' | 'alarms' | 'datapoint' | 'anomaly';

export function CategoryGlyph({ kind, size = 15 }: { kind: GlyphKind; size?: number }) {
  const common = {
    width: size, height: size, viewBox: '0 0 16 16',
    fill: 'none', stroke: 'currentColor', 'aria-hidden': true,
    focusable: false as const,
  };

  switch (kind) {
    // Thermometer: stem and bulb.
    case 'thermal':
      return (
        <svg {...common}>
          <rect x="6" y="1" width="4" height="9" rx="2" fill="currentColor" stroke="none" />
          <circle cx="8" cy="12" r="4" fill="currentColor" stroke="none" />
        </svg>
      );

    // Signal bars: reachability.
    case 'connectivity':
      return (
        <svg {...common}>
          {[3, 6, 9, 12].map((h, i) => (
            <rect key={h} x={1 + i * 4} y={15 - h} width="2.4" height={h}
                  rx="1" fill="currentColor" stroke="none" />
          ))}
        </svg>
      );

    // Warning triangle: a plain alarm.
    case 'alarms':
      return (
        <svg {...common}>
          <path d="M8 2 L15 14 H1 Z" fill="currentColor" stroke="none" />
        </svg>
      );

    // Ring with a bar: the reading is absent, not bad.
    case 'datapoint':
      return (
        <svg {...common} strokeWidth="1.6">
          <circle cx="8" cy="8" r="6.2" />
          <line x1="4.8" y1="8" x2="11.2" y2="8" strokeLinecap="round" />
        </svg>
      );

    // Ragged trace: a shape that is out of pattern.
    case 'anomaly':
      return (
        <svg {...common}>
          {[4, 8, 6, 12].map((h, i) => (
            <rect key={i} x={1 + i * 4} y={15 - h} width="2.4" height={h}
                  rx="1" fill="currentColor" stroke="none" />
          ))}
        </svg>
      );
  }
}
