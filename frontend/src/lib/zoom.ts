/**
 * The page's CSS zoom (compact density, index.css: 0.85 on a laptop-sized
 * window, otherwise 1).
 *
 * Under a zoomed root, Chrome reports mouse positions and
 * getBoundingClientRect() in SCREEN pixels, while `left`, `top` and `width`
 * written in CSS are in the page's own, unzoomed pixels. Anything that takes a
 * position or size from the one and writes it to the other goes through
 * `toCss`, or it lands at 85 % of where it should.
 */
export function pageZoom(): number {
  if (typeof document === 'undefined') return 1;
  const z = parseFloat(getComputedStyle(document.documentElement).zoom || '1');
  return Number.isFinite(z) && z > 0 ? z : 1;
}

/** Screen pixels (clientX, a client rect, innerWidth) to the page's CSS pixels. */
export function toCss(px: number): number {
  return px / pageZoom();
}
