import { useEffect, useRef, useState } from 'react';

/**
 * How the picture is driven, behind a round "?" at the bottom right: the
 * status bar used to carry the hints as a permanent line of text. The card
 * closes on Escape, on a click outside, and on the next view change.
 */
export function Help({ lines, keyName }: { lines: string[]; keyName: string }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => { setOpen(false); }, [keyName]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    const onDown = (e: PointerEvent) => { if (root.current && !root.current.contains(e.target as Node)) setOpen(false); };
    window.addEventListener('keydown', onKey);
    window.addEventListener('pointerdown', onDown);
    return () => { window.removeEventListener('keydown', onKey); window.removeEventListener('pointerdown', onDown); };
  }, [open]);
  return (
    <div className="vw-help" ref={root}>
      {open && (
        <div className="vw-help-card" role="dialog" aria-label="How to move around">
          <h4>How to move around</h4>
          <ul>{lines.map((l) => <li key={l}>{l}</li>)}</ul>
        </div>
      )}
      <button type="button" className={`vw-round${open ? ' is-on' : ''}`} onClick={() => setOpen((o) => !o)}
              aria-expanded={open} aria-label="How to move around" title="How to move around">
        <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6"
             strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <circle cx="10" cy="10" r="7.5" /><path d="M7.6 7.8a2.4 2.4 0 1 1 3.4 2.2c-.7.4-1 .9-1 1.6" />
          <circle cx="10" cy="14.3" r="0.9" fill="currentColor" stroke="none" />
        </svg>
      </button>
    </div>
  );
}
