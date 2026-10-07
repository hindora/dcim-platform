import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { NavLink, useLocation } from 'react-router-dom';

export interface NavMenuItem {
  to: string;
  label: string;
  /** One line under the label: what the view answers. */
  hint: string;
}

/**
 * A top-bar entry that opens a list of views instead of navigating.
 *
 * The panel is portalled to <body> and placed under the trigger: the nav row
 * scrolls horizontally on a narrow window (overflow-x: auto), and an absolutely
 * positioned panel inside it would be clipped to the row's height. The trigger
 * is underlined like an active link while any of its views is open.
 */
export function NavMenu({ label, items }: { label: string; items: NavMenuItem[] }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const location = useLocation();
  const active = items.some((i) => location.pathname === i.to
    || location.pathname.startsWith(`${i.to}/`));

  // Any navigation closes it - including one made from inside the panel.
  useEffect(() => { setOpen(false); }, [location.pathname]);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!panel.current?.contains(t) && !trigger.current?.contains(t)) setOpen(false);
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { setOpen(false); trigger.current?.focus(); }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        const links = Array.from(panel.current?.querySelectorAll('a') ?? []);
        if (!links.length) return;
        const i = links.indexOf(document.activeElement as HTMLAnchorElement);
        const next = e.key === 'ArrowDown' ? (i + 1) % links.length
          : (i <= 0 ? links.length - 1 : i - 1);
        links[next].focus();
        e.preventDefault();
      }
    };
    const place = () => {
      const r = trigger.current?.getBoundingClientRect();
      if (r) setPos({ left: r.left, top: r.bottom });
    };
    place();
    document.addEventListener('mousedown', away);
    document.addEventListener('keydown', key);
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => {
      document.removeEventListener('mousedown', away);
      document.removeEventListener('keydown', key);
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
    };
  }, [open]);

  return (
    <>
      <button ref={trigger} type="button"
              className={`nav-trigger${active ? ' active' : ''}${open ? ' open' : ''}`}
              aria-haspopup="menu" aria-expanded={open}
              onClick={() => setOpen((o) => !o)}>
        {label}<span className="chev" aria-hidden>▾</span>
      </button>
      {open && pos && createPortal(
        <div ref={panel} className="nav-menu" role="menu" aria-label={label}
             style={{ left: pos.left, top: pos.top }}>
          {items.map((i) => (
            <NavLink key={i.to} to={i.to} role="menuitem">
              <span className="label">{i.label}</span>
              <span className="hint">{i.hint}</span>
            </NavLink>
          ))}
        </div>,
        document.body,
      )}
    </>
  );
}
