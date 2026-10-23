import { Fragment, useEffect, useId, useRef, useState } from 'react';
import { alarmColor } from './colors';

/**
 * Where you are, as a trail: Compound › Building › Room › Row › Rack, the
 * way DCIM tools lay out a location. Every segment above the current one is
 * a step back up in one click. Each segment's caret lists its siblings - the
 * other halls on the floor, the other racks in the row - so moving across
 * does not mean climbing up first.
 *
 * The navigator in the dock still shows the whole estate as a tree; this
 * shows the one path through it and how to step along it.
 */

export interface CrumbItem {
  id: string;
  label: string;
  /** A short fact beside the name: rack count, level, room class. */
  meta?: string;
  /** Worst open alarm, drawn as a swatch; absent = no swatch. */
  severity?: string;
  current?: boolean;
}

export interface Crumb {
  key: string;
  /** What kind of place this is: Compound, Building, Room, Row, Rack. */
  rung: string;
  name: string;
  /** Going there. Absent on the current segment and on a place with no view of its own (a row). */
  onGo?: () => void;
  /** Its siblings, to move across without climbing first. */
  menu?: { label: string; items: CrumbItem[]; onPick: (id: string) => void };
}

function Chevron() {
  return (
    <svg className="vw-crumb-sep" width="8" height="12" viewBox="0 0 8 12" aria-hidden>
      <path d="M2 1.5 L6 6 L2 10.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function Caret() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
      <path d="M2 3.5 L5 6.5 L8 3.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function Siblings({ id, label, items, onPick, onClose }: {
  id: string; label: string; items: CrumbItem[]; onPick: (id: string) => void; onClose: (refocus: boolean) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  // Open on the current place, so the arrow keys start from where you are.
  useEffect(() => {
    const btns = ref.current?.querySelectorAll<HTMLButtonElement>('button');
    if (!btns?.length) return;
    const cur = items.findIndex((x) => x.current);
    btns[Math.max(0, cur)].focus();
    btns[Math.max(0, cur)].scrollIntoView({ block: 'nearest' });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const away = (e: PointerEvent) => {
      const wrap = ref.current?.parentElement;
      if (wrap && !wrap.contains(e.target as Node)) onClose(false);
    };
    window.addEventListener('pointerdown', away);
    return () => window.removeEventListener('pointerdown', away);
  }, [onClose]);
  const onKey = (e: React.KeyboardEvent) => {
    // The viewer climbs a rung on Escape and Backspace; inside the list they
    // belong to the list.
    e.stopPropagation();
    const btns = [...(ref.current?.querySelectorAll<HTMLButtonElement>('button') ?? [])];
    const at = btns.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === 'Escape') { e.preventDefault(); onClose(true); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); btns[(at + 1) % btns.length]?.focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); btns[(at - 1 + btns.length) % btns.length]?.focus(); }
    else if (e.key === 'Home') { e.preventDefault(); btns[0]?.focus(); }
    else if (e.key === 'End') { e.preventDefault(); btns[btns.length - 1]?.focus(); }
    else if (e.key === 'Tab') onClose(false);
  };
  return (
    <div className="vw-crumb-menu" id={id} role="menu" aria-label={label} ref={ref} onKeyDown={onKey}>
      {items.map((it) => (
        <button key={it.id} type="button" role="menuitemradio" aria-checked={Boolean(it.current)}
                className={it.current ? 'is-current' : undefined}
                onClick={() => { onClose(false); if (!it.current) onPick(it.id); }}>
          {it.severity !== undefined
            ? <span className="swatch" style={{ background: alarmColor(it.severity, 0) }}
                    title={it.severity === 'CLEAR' ? 'No open alarm' : `Worst open alarm: ${it.severity.toLowerCase()}`} />
            : <span />}
          <span className="name">{it.label}</span>
          <span className="meta">{it.meta ?? ''}</span>
        </button>
      ))}
    </div>
  );
}

export function Crumbs({ crumbs }: { crumbs: Crumb[] }) {
  const [open, setOpen] = useState<string | null>(null);
  const base = useId();
  const triggers = useRef<Record<string, HTMLButtonElement | null>>({});
  // A new place closes whatever list was open.
  const path = crumbs.map((c) => `${c.key}:${c.name}`).join('/');
  useEffect(() => { setOpen(null); }, [path]);
  const last = crumbs.length - 1;
  return (
    <nav className="vw-crumbs" aria-label="Location">
      <ol>
        {crumbs.map((c, i) => {
          const isLast = i === last;
          const mid = i > 0 && i < last;
          const menuId = `${base}-${c.key}`;
          const hasMenu = Boolean(c.menu && c.menu.items.length > 1);
          const isOpen = open === c.key && hasMenu;
          const toggle = () => setOpen((o) => (o === c.key ? null : c.key));
          const close = (refocus: boolean) => {
            setOpen(null);
            if (refocus) triggers.current[c.key]?.focus();
          };
          // The current place, and a place with no view of its own, open
          // their list from the name; anywhere else the name goes there.
          const nameGoes = Boolean(c.onGo) && !isLast;
          return (
            <Fragment key={c.key}>
            {/* Narrow screens keep the first place and where you are; the rest fold into this. */}
            {i === 1 && last > 1 && <li className="vw-crumb-gap" aria-hidden><Chevron /><span>…</span></li>}
            <li className={`vw-crumb${isLast ? ' is-here' : ''}${mid ? ' is-mid' : ''}`}>
              {i > 0 && <Chevron />}
              <div className="vw-crumb-seg">
                {nameGoes
                  ? <button type="button" className="vw-crumb-name" onClick={c.onGo} title={`Go to ${c.name}`}>
                      <small>{c.rung}</small><b><span className="t">{c.name}</span></b>
                    </button>
                  : hasMenu
                    ? <button type="button" className="vw-crumb-name" aria-haspopup="menu" aria-expanded={isOpen}
                              aria-controls={isOpen ? menuId : undefined} aria-current={isLast ? 'location' : undefined}
                              ref={(el) => { triggers.current[c.key] = el; }} onClick={toggle}
                              title={`${c.menu!.label}`}>
                        <small>{c.rung}</small><b><span className="t">{c.name}</span><Caret /></b>
                      </button>
                    : <span className="vw-crumb-name is-static" aria-current={isLast ? 'location' : undefined}>
                        <small>{c.rung}</small><b><span className="t">{c.name}</span></b>
                      </span>}
                {nameGoes && hasMenu && (
                  <button type="button" className="vw-crumb-caret" aria-haspopup="menu" aria-expanded={isOpen}
                          aria-controls={isOpen ? menuId : undefined} aria-label={c.menu!.label}
                          ref={(el) => { triggers.current[c.key] = el; }} onClick={toggle} title={c.menu!.label}>
                    <Caret />
                  </button>
                )}
                {isOpen && c.menu && (
                  <Siblings id={menuId} label={c.menu.label} items={c.menu.items} onPick={c.menu.onPick} onClose={close} />
                )}
              </div>
            </li>
            </Fragment>
          );
        })}
      </ol>
    </nav>
  );
}
