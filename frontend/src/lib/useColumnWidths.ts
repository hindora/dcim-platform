/** Drag-resizable table columns, remembered per browser.
 *
 *  Operators do not all read the same column. One watches room names that run
 *  to forty characters, another wants eight status tiles and nothing else on
 *  screen. A fixed layout picks a winner; this lets each of them settle it for
 *  themselves, and remembers the answer so they do not settle it again every
 *  morning.
 *
 *  Widths live in `localStorage` rather than on the server: it is a per-person,
 *  per-screen preference, and a round trip to store it would make the table
 *  wait on the network to know how wide it is.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

/** Below this a column stops being a column and becomes a sliver. */
const MIN_WIDTH = 44;

export interface ColumnWidths {
  width: (key: string, fallback: number) => number;
  /** Start a drag from a header grip.
   *
   *  `seed` reports what every column is currently rendering at. The first
   *  drag pins all of them, because until then one column is taking whatever
   *  space is left - and widening its neighbour would eat that column alive
   *  rather than making the table wider. */
  begin: (key: string, fallback: number, seed?: () => Record<string, number>)
    => (e: React.PointerEvent) => void;
  /** Back to the layout the page shipped with. */
  reset: () => void;
  /** One column back to its shipped width - double-clicking its grip. */
  resetOne: (key: string) => void;
  resizing: string | null;
  dirty: boolean;
}

export function useColumnWidths(storageKey: string): ColumnWidths {
  const [widths, setWidths] = useState<Record<string, number>>(() => {
    try {
      return JSON.parse(localStorage.getItem(storageKey) ?? '{}');
    } catch {
      // A corrupt entry is not worth a broken table.
      return {};
    }
  });
  const [resizing, setResizing] = useState<string | null>(null);
  const drag = useRef<{ key: string; startX: number; startW: number } | null>(null);

  useEffect(() => {
    try {
      if (Object.keys(widths).length) {
        localStorage.setItem(storageKey, JSON.stringify(widths));
      } else {
        localStorage.removeItem(storageKey);
      }
    } catch {
      // Private browsing, quota, a locked profile - none of it is a reason to
      // stop rendering a table.
    }
  }, [storageKey, widths]);

  useEffect(() => {
    if (!resizing) return undefined;

    const move = (e: PointerEvent) => {
      const d = drag.current;
      if (!d) return;
      const next = Math.max(MIN_WIDTH, Math.round(d.startW + (e.clientX - d.startX)));
      setWidths((w) => ({ ...w, [d.key]: next }));
    };
    const up = () => { drag.current = null; setResizing(null); };

    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    // The cursor and the text selection follow the drag, not the element the
    // pointer happens to be over - otherwise dragging left over a row selects
    // its text and the table looks like it is glitching.
    const prev = document.body.style.cursor;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    return () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      document.body.style.cursor = prev;
      document.body.style.userSelect = '';
    };
  }, [resizing]);

  const begin = useCallback((
    key: string, fallback: number, seed?: () => Record<string, number>,
  ) => (e: React.PointerEvent) => {
    e.preventDefault();
    e.stopPropagation();

    // Pin the layout as it stands before the first drag. Without this the
    // flexible column absorbs every pixel the others gain and collapses to an
    // ellipsis, which looks like a bug and loses the reader their room names.
    const measured = seed?.() ?? {};
    const startW = widths[key] ?? measured[key] ?? fallback;
    setWidths((w) => {
      const next = { ...w };
      for (const [k, px] of Object.entries(measured)) {
        if (next[k] === undefined) next[k] = Math.round(px);
      }
      next[key] = Math.round(startW);
      return next;
    });

    drag.current = { key, startX: e.clientX, startW };
    setResizing(key);
  }, [widths]);

  const width = useCallback(
    (key: string, fallback: number) => widths[key] ?? fallback, [widths]);

  const reset = useCallback(() => setWidths({}), []);

  const resetOne = useCallback((key: string) => setWidths((w) => {
    const next = { ...w };
    delete next[key];
    return next;
  }), []);

  return {
    width, begin, reset, resetOne, resizing,
    dirty: Object.keys(widths).length > 0,
  };
}
