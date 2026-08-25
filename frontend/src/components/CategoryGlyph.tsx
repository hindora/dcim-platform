/**
 * One glyph per alert category.
 *
 * These exist so the indicator cells are not distinguished by colour alone.
 * An operator with deuteranopia, and a runbook printed in black and white,
 * both still get the category from the shape.
 *
 * That matters more since the taxonomy went to eight categories: the palette
 * carries five hues, one per strip group, so the two categories inside a group
 * SHARE a colour and are told apart by shape alone. Colour says who owns it,
 * shape says what it is.
 *
 * Drawn rather than pulled from an icon font: the home page renders one per
 * category per row, and a font that fails to load would leave a grid of
 * tofu boxes where the estate's health is supposed to be.
 */

import type { AlertCategory } from '../api/client';

/** The eight categories, plus `alarms` for the all-categories total. */
export type GlyphKind = AlertCategory | 'alarms';

export function CategoryGlyph({ kind, size = 15 }: { kind: GlyphKind; size?: number }) {
  const common = {
    width: size, height: size, viewBox: '0 0 16 16',
    fill: 'none', stroke: 'currentColor', 'aria-hidden': true,
    focusable: false as const,
  };

  switch (kind) {
    // Warning triangle: any alarm, of any category.
    case 'alarms':
      return (
        <svg {...common}>
          <path d="M8 2 L15 14 H1 Z" fill="currentColor" stroke="none" />
        </svg>
      );

    // An eye, struck through. Not "something is broken" but "we cannot see" -
    // the equipment behind a visibility alarm may be perfectly healthy.
    case 'visibility':
      return (
        <svg {...common} strokeWidth="1.5">
          <path d="M1 8s2.6-4 7-4 7 4 7 4-2.6 4-7 4-7-4-7-4Z" />
          <circle cx="8" cy="8" r="1.7" fill="currentColor" stroke="none" />
          <line x1="2.5" y1="13.5" x2="13.5" y2="2.5" strokeLinecap="round" />
        </svg>
      );

    // Air over a floor: the space itself, not the machines standing in it.
    case 'environmental':
      return (
        <svg {...common} strokeWidth="1.5" strokeLinecap="round">
          <path d="M1.5 4.5c1.4-1.6 3-1.6 4.4 0s3 1.6 4.4 0 3-1.6 4.2 0" />
          <path d="M1.5 8c1.4-1.6 3-1.6 4.4 0s3 1.6 4.4 0 3-1.6 4.2 0" />
          <line x1="1.5" y1="13" x2="14.5" y2="13" />
        </svg>
      );

    // Snowflake: the plant that removes the heat.
    case 'cooling':
      return (
        <svg {...common} strokeWidth="1.5" strokeLinecap="round">
          <line x1="8" y1="1.5" x2="8" y2="14.5" />
          <line x1="2.4" y1="4.7" x2="13.6" y2="11.3" />
          <line x1="2.4" y1="11.3" x2="13.6" y2="4.7" />
        </svg>
      );

    // Bolt: the electrical chain.
    case 'power':
      return (
        <svg {...common}>
          <path d="M9.4 1 3.5 9h3.4l-1 6 6-8.4H8.5L9.4 1Z"
                fill="currentColor" stroke="none" />
        </svg>
      );

    // A chip: one host and its parts.
    case 'it_equipment':
      return (
        <svg {...common} strokeWidth="1.4">
          <rect x="4" y="4" width="8" height="8" rx="1" />
          <rect x="6.6" y="6.6" width="2.8" height="2.8" fill="currentColor" stroke="none" />
          {[6, 10].map((x) => (
            <g key={x}>
              <line x1={x} y1="1.5" x2={x} y2="4" strokeLinecap="round" />
              <line x1={x} y1="12" x2={x} y2="14.5" strokeLinecap="round" />
              <line x1="1.5" y1={x} x2="4" y2={x} strokeLinecap="round" />
              <line x1="12" y1={x} x2="14.5" y2={x} strokeLinecap="round" />
            </g>
          ))}
        </svg>
      );

    // Nodes and links: the fabric between the hosts.
    case 'network':
      return (
        <svg {...common} strokeWidth="1.4">
          <line x1="8" y1="4" x2="3.5" y2="12" />
          <line x1="8" y1="4" x2="12.5" y2="12" />
          <circle cx="8" cy="3" r="2.1" fill="currentColor" stroke="none" />
          <circle cx="3" cy="13" r="2.1" fill="currentColor" stroke="none" />
          <circle cx="13" cy="13" r="2.1" fill="currentColor" stroke="none" />
        </svg>
      );

    // A gauge near its stop: headroom, not a fault.
    case 'capacity':
      return (
        <svg {...common} strokeWidth="1.6" strokeLinecap="round">
          <path d="M2 12a6 6 0 1 1 12 0" />
          <line x1="8" y1="12" x2="12" y2="8.4" />
        </svg>
      );

    // A question: nobody has classified this yet, and the gap is the point.
    case 'uncategorised':
      return (
        <svg {...common} strokeWidth="1.5" strokeLinecap="round">
          <circle cx="8" cy="8" r="6.4" />
          <path d="M6.2 6.1a1.9 1.9 0 1 1 2.6 1.8c-.5.2-.8.7-.8 1.2v.4" />
          <circle cx="8" cy="11.7" r=".85" fill="currentColor" stroke="none" />
        </svg>
      );
  }
}
