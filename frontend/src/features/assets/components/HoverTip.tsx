import { useState, type ReactNode } from 'react';

/** A cursor-anchored tooltip card for chart marks, replacing the browser's
 *  native title balloon - which cannot be styled, waits a full second, and
 *  reads as a different product from the page around it.
 *
 *  One hook per chart: `bind(content)` spreads mouse handlers onto a mark,
 *  and `tipEl` renders the single card the marks share. The card is
 *  position: fixed so no scroll container can clip it, follows the cursor,
 *  and is pointer-events: none so it never traps the hover that opened it. */
export function useHoverTip() {
  const [tip, setTip] = useState<{ x: number; y: number; node: ReactNode } | null>(null);

  const bind = (node: ReactNode) => ({
    onMouseEnter: (e: React.MouseEvent) =>
      setTip({ x: e.clientX, y: e.clientY, node }),
    onMouseMove: (e: React.MouseEvent) =>
      setTip((t) => (t ? { ...t, x: e.clientX, y: e.clientY } : t)),
    onMouseLeave: () => setTip(null),
  });

  const tipEl = tip ? (
    <span
      className="asset-hover-tip"
      role="tooltip"
      style={{
        top: tip.y + 16,
        // Clamped so a mark near the right edge does not push the card
        // off-screen.
        left: Math.min(tip.x + 12, window.innerWidth - 230),
      }}
    >
      {tip.node}
    </span>
  ) : null;

  return { bind, tipEl };
}
