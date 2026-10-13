import { useEffect, useState } from 'react';

/**
 * The viewer's dock: a column of icon buttons on the left edge that opens
 * the side panels - information, layers, filters - with a chevron at the top
 * that folds the column away to the chevron alone. One place for every
 * panel the viewer has, at every rung, so the hand learns one spot.
 *
 * Each button is a toggle with its name on it for the screen reader and in
 * the tooltip; the pressed state is the panel that is open. The fold is a
 * per-viewer convenience kept in local storage and nothing depends on it.
 */

export type DockItem = 'info' | 'layers' | 'filters';

const FOLD_KEY = 'dcim.viewer.dock';

function Glyph({ kind }: { kind: DockItem | 'fold' | 'unfold' }) {
  const common = { width: 18, height: 18, viewBox: '0 0 20 20', fill: 'none', stroke: 'currentColor',
                   strokeWidth: 1.6, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const, 'aria-hidden': true };
  switch (kind) {
    case 'fold': return <svg {...common}><path d="M12 5l-5 5 5 5" /></svg>;
    case 'unfold': return <svg {...common}><path d="M8 5l5 5-5 5" /></svg>;
    case 'info': return (
      <svg {...common}><circle cx="10" cy="10" r="7.5" /><path d="M10 9v5" /><circle cx="10" cy="6.3" r="0.9" fill="currentColor" stroke="none" /></svg>
    );
    case 'layers': return (
      <svg {...common}><path d="M10 3.5l7 3.8-7 3.8-7-3.8z" /><path d="M3 10.6l7 3.8 7-3.8" /><path d="M3 13.9l7 3.8 7-3.8" /></svg>
    );
    case 'filters': return (
      <svg {...common}>
        <path d="M3 6h14M3 10h14M3 14h14" />
        <circle cx="7" cy="6" r="1.6" fill="var(--bg-raised)" /><circle cx="13" cy="10" r="1.6" fill="var(--bg-raised)" /><circle cx="8.5" cy="14" r="1.6" fill="var(--bg-raised)" />
      </svg>
    );
  }
}

const WORDS: Record<DockItem, string> = { info: 'Information', layers: 'Layers', filters: 'Filters' };

export function Dock({ items, open, onToggle }: {
  /** The panels this rung offers, in dock order. */
  items: DockItem[];
  /** The one that is open, if any. */
  open: DockItem | null;
  onToggle: (item: DockItem) => void;
}) {
  const [folded, setFolded] = useState<boolean>(() => {
    try { return localStorage.getItem(FOLD_KEY) === 'folded'; } catch { return false; }
  });
  useEffect(() => { try { localStorage.setItem(FOLD_KEY, folded ? 'folded' : 'open'); } catch { /* a private window */ } }, [folded]);
  return (
    <nav className={`vw-dock${folded ? ' is-folded' : ''}`} aria-label="Viewer panels">
      <button type="button" className="dock-fold" onClick={() => setFolded((f) => !f)} aria-expanded={!folded}
              aria-label={folded ? 'Show the panel buttons' : 'Hide the panel buttons'}
              title={folded ? 'Show the panel buttons' : 'Hide the panel buttons'}>
        <Glyph kind={folded ? 'unfold' : 'fold'} />
      </button>
      {items.map((it) => (
        <button key={it} type="button" className={open === it ? 'is-on' : undefined} aria-pressed={open === it}
                aria-label={WORDS[it]} title={WORDS[it]} onClick={() => onToggle(it)}>
          <Glyph kind={it} />
        </button>
      ))}
    </nav>
  );
}
