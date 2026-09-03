import { useEffect, useRef, type ReactNode } from 'react';

/** A modal dialog, module-local.
 *
 *  Forked into features/assets/ rather than added to a shared component,
 *  because there is no shared dialog and adding one would put a new file in a
 *  directory three out-of-scope pages import from (docs/22 §1).
 *
 *  It is a real dialog, not a div that looks like one: Escape closes it, focus
 *  moves into it on open and returns to whatever opened it on close, Tab is
 *  trapped inside, and the backdrop click is deliberately NOT a close - a form
 *  with typing in it should not be dismissed by a stray click near the edge.
 */
export function Dialog({ title, children, onClose, wide = false }: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const opener = useRef<Element | null>(null);

  useEffect(() => {
    opener.current = document.activeElement;
    // Focus the panel rather than the first field: a screen reader should hear
    // the dialog's name before it hears "Title, edit text".
    panel.current?.focus();

    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== 'Tab' || !panel.current) return;
      const focusable = panel.current.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), '
        + 'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])');
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }

    document.addEventListener('keydown', onKey, true);
    return () => {
      document.removeEventListener('keydown', onKey, true);
      (opener.current as HTMLElement | null)?.focus?.();
    };
  }, [onClose]);

  return (
    <div className="asset-backdrop">
      <div
        className={`asset-dialog${wide ? ' is-wide' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        ref={panel}
      >
        <div className="asset-dialog-head">
          <h3>{title}</h3>
          <button type="button" className="asset-dialog-close"
                  onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <div className="asset-dialog-body">{children}</div>
      </div>
    </div>
  );
}

/** The row of actions at the foot of a dialog. Destructive on the left, the
 *  confirming action on the right, so the pointer never rests on "delete" on
 *  the way to "save". */
export function DialogActions({ children }: { children: ReactNode }) {
  return <div className="asset-dialog-actions">{children}</div>;
}
