import { useEffect, useState } from 'react';

/**
 * The viewer's dock: a column of round buttons floating on the left edge,
 * with a "more" button at the top that folds the column away to itself. One
 * place for everything the viewer offers besides the picture and the way
 * it is looked at - the top centre keeps the camera and 3D / PLAN / FPV.
 *
 * Two kinds of button, told apart by a gap between them: the panels
 * (navigate, information, filters, layers, search), which are toggles whose
 * pressed state is the panel that is open; and the tools under the gap - the
 * temperature unit - which act at once.
 *
 * Each button carries its name for the screen reader and the tooltip. The
 * fold is a per-browser convenience kept in local storage.
 */

export type DockItem = 'navigate' | 'info' | 'filters' | 'layers' | 'search';

export interface DockTool {
  id: string;
  label: string;
  /** A glyph, or short text (a unit) drawn in its place. */
  glyph?: 'reset';
  text?: string;
  onClick: () => void;
}

const FOLD_KEY = 'dcim.viewer.dock';

function Glyph({ kind }: { kind: DockItem | 'more' | 'less' | 'reset' }) {
  const common = { width: 18, height: 18, viewBox: '0 0 20 20', fill: 'none', stroke: 'currentColor',
                   strokeWidth: 1.6, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const, 'aria-hidden': true };
  switch (kind) {
    case 'more': return (
      <svg {...common}><circle cx="10" cy="4.5" r="1.3" fill="currentColor" stroke="none" />
        <circle cx="10" cy="10" r="1.3" fill="currentColor" stroke="none" /><circle cx="10" cy="15.5" r="1.3" fill="currentColor" stroke="none" /></svg>
    );
    case 'less': return <svg {...common}><path d="M6 6l8 8M14 6l-8 8" /></svg>;
    case 'navigate': return (
      <svg {...common}><rect x="7.5" y="2.5" width="5" height="4" rx="1" /><rect x="2" y="13" width="5" height="4" rx="1" /><rect x="13" y="13" width="5" height="4" rx="1" />
        <path d="M10 6.5V10M4.5 13v-3h11v3" /></svg>
    );
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
    case 'search': return (
      <svg {...common}><circle cx="8.5" cy="8.5" r="5" /><path d="M12.3 12.3L17 17" /></svg>
    );
    case 'reset': return (
      <svg {...common}><circle cx="10" cy="10" r="5.5" /><path d="M10 2.5v3M10 14.5v3M2.5 10h3M14.5 10h3" /><circle cx="10" cy="10" r="1" fill="currentColor" stroke="none" /></svg>
    );
  }
}

const WORDS: Record<DockItem, string> = { navigate: 'Navigate', info: 'Information', filters: 'Filters', layers: 'Layers', search: 'Search' };

export function Dock({ items, open, onToggle, tools = [] }: {
  /** The panels this rung offers, in dock order. */
  items: DockItem[];
  /** The one that is open, if any. */
  open: DockItem | null;
  onToggle: (item: DockItem) => void;
  /** What acts at once, under the rule. */
  tools?: DockTool[];
}) {
  const [folded, setFolded] = useState<boolean>(() => {
    try { return localStorage.getItem(FOLD_KEY) === 'folded'; } catch { return false; }
  });
  useEffect(() => { try { localStorage.setItem(FOLD_KEY, folded ? 'folded' : 'open'); } catch { /* a private window */ } }, [folded]);
  return (
    <nav className={`vw-dock${folded ? ' is-folded' : ''}`} aria-label="Viewer panels and tools">
      <button type="button" className="vw-round dock-fold" onClick={() => setFolded((f) => !f)} aria-expanded={!folded}
              aria-label={folded ? 'Show the tools' : 'Hide the tools'} title={folded ? 'Show the tools' : 'Hide the tools'}>
        <Glyph kind={folded ? 'more' : 'less'} />
      </button>
      {items.map((it) => (
        <button key={it} type="button" className={`vw-round${open === it ? ' is-on' : ''}`} aria-pressed={open === it}
                aria-label={WORDS[it]} title={WORDS[it]} onClick={() => onToggle(it)}>
          <Glyph kind={it} />
        </button>
      ))}
      {tools.length > 0 && <span className="dock-gap" aria-hidden />}
      {tools.map((t) => (
        <button key={t.id} type="button" className="vw-round dock-tool" aria-label={t.label} title={t.label} onClick={t.onClick}>
          {t.text != null ? <span className="dock-text">{t.text}</span> : <Glyph kind={t.glyph ?? 'reset'} />}
        </button>
      ))}
    </nav>
  );
}
