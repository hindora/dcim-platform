/** The colours and stroke patterns a multi-series chart draws with.
 *
 *  The values live in `index.css` as `--series-1..4`, per theme, like every
 *  other colour in this app - the charts used to carry their own hex array,
 *  which is how they ended up theme-blind (one ramp on both palettes, and
 *  status hues that stayed at their dark-theme values on white) and how
 *  `#3b82f6` blue and `#a855f7` purple came to sit in slots 1 and 3. A
 *  deuteranope sees those two as #7373f6 and #7575f6: a CIEDE2000 distance of
 *  0.6, which is one colour, on every three-series chart in the product.
 *  `scripts/validate_palette.js` now fails CI if a future edit closes a gap.
 *
 *  Resolved at render rather than imported as constants, because the palette
 *  can change under a running page: `lib/theme.ts` stamps `data-theme` on
 *  <html>, and a wall display set to follow the system does it at dusk with
 *  nobody there to reload.
 *
 *  COLOUR IS NEVER THE ONLY CHANNEL. From the third series on, the line is
 *  also dashed. Hue fails in more ways than colour blindness - a dim wall
 *  panel, a projector, a greyscale printout in a runbook, a photograph of a
 *  screen in an incident ticket - and a dash pattern survives all of them.
 */
import { useEffect, useState } from 'react';

/** Read if the stylesheet has not loaded or the document is not there (tests,
 *  server rendering). The dark theme's values, since dark is the default. */
const FALLBACK = {
  series: ['#4aa3df', '#ef7d2e', '#7fe3c4', '#b4628f'],
  ok: '#2ea043',
  warn: '#d29922',
  critical: '#f85149',
  muted: '#6e7681',
};

/** Solid, solid, then patterns. The first two lines are the common case and
 *  keep the cleanest stroke; a chart only reaches for a pattern once hue is
 *  carrying more than it can. `undefined` rather than `'none'` so the value
 *  can go straight into strokeDasharray. */
export const DASHES: (string | undefined)[] = [undefined, undefined, '6 3', '2 3'];

export interface ChartColors {
  /** Four, in slot order. The chart rules say to facet past three series, so
   *  the fourth is a spare rather than an invitation to a fifth. */
  series: string[];
  /** The single-series accent: slot one, so one line and the first of many
   *  are the same colour. */
  primary: string;
  /** A projection or a modelled band - slot four, which is the ramp's most
   *  distant member from the measured line it sits beside. */
  projection: string;
  ok: string;
  warn: string;
  critical: string;
  muted: string;
}

function read(): ChartColors {
  if (typeof document === 'undefined' || !document.documentElement) {
    return { ...FALLBACK, primary: FALLBACK.series[0], projection: FALLBACK.series[3] };
  }
  const cs = getComputedStyle(document.documentElement);
  const v = (name: string, fb: string) => cs.getPropertyValue(name).trim() || fb;
  const series = FALLBACK.series.map((fb, i) => v(`--series-${i + 1}`, fb));
  return {
    series,
    primary: series[0],
    projection: series[3],
    ok: v('--ok', FALLBACK.ok),
    warn: v('--warn', FALLBACK.warn),
    critical: v('--critical', FALLBACK.critical),
    muted: v('--unknown', FALLBACK.muted),
  };
}

/** The resolved palette, re-read whenever <html> changes theme.
 *
 *  One observer for the whole app rather than one per chart: a page with six
 *  panels on it would otherwise hold six observers watching one attribute. */
let current: ChartColors | null = null;
const listeners = new Set<() => void>();

function refresh() {
  current = read();
  listeners.forEach((fn) => fn());
}

function subscribe(fn: () => void): () => void {
  if (listeners.size === 0 && typeof MutationObserver !== 'undefined') {
    observer = new MutationObserver(refresh);
    observer.observe(document.documentElement, {
      attributes: true, attributeFilter: ['data-theme'],
    });
  }
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
    if (listeners.size === 0 && observer) {
      observer.disconnect();
      observer = null;
    }
  };
}

let observer: MutationObserver | null = null;

export function useChartColors(): ChartColors {
  const [colors, setColors] = useState<ChartColors>(() => current ?? read());
  useEffect(() => subscribe(() => setColors(read())), []);
  return colors;
}
