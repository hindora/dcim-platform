import { useEffect, type ReactNode } from 'react';

/** A full-window view of one chart.
 *
 *  The modal renders the SAME elements the panel does - the filters and the
 *  chart - so a filter chosen in either place is the one state, and the big
 *  view is never a stale copy. Escape and a backdrop click both close it,
 *  and the page behind stops scrolling while it is up.
 */
/** The maximize glyph: two arrows pulling opposite corners apart. Drawn
 *  rather than a dingbat character, so it renders the same on every font. */
export function MaxGlyph() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" aria-hidden
         fill="none" stroke="currentColor" strokeWidth="1.6"
         strokeLinecap="round" strokeLinejoin="round">
      <path d="M9.5 2.5h4v4" />
      <path d="M13.5 2.5 9.2 6.8" />
      <path d="M6.5 13.5h-4v-4" />
      <path d="M2.5 13.5 6.8 9.2" />
    </svg>
  );
}

export function MaxModal({ title, onClose, children }: {
  title: string; onClose: () => void; children: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);

  return (
    <div className="max-modal" role="dialog" aria-modal="true" aria-label={title}
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="max-modal-card">
        <header className="max-modal-head">
          <h2>{title}</h2>
          <button type="button" onClick={onClose} aria-label="Close">✕</button>
        </header>
        <div className="max-modal-body">{children}</div>
      </div>
    </div>
  );
}
