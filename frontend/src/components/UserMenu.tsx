import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

/** The account menu behind USER: ADMIN.
 *
 *  The chevron beside the name used to sign the user out on the first click,
 *  which is the one action in the menu that cannot be undone by clicking again.
 *  Now it opens, and signing out is a deliberate choice inside. */
export function UserMenu({ username, onSignOut }: {
  username: string;
  onSignOut: () => void;
}) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();

  // Close on a click anywhere else, and on Escape. Both are what a menu is
  // expected to do; without them the panel outlives whatever the reader went
  // on to click.
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (!box.current?.contains(e.target as Node)) setOpen(false);
    };
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', away);
    document.addEventListener('keydown', key);
    return () => {
      document.removeEventListener('mousedown', away);
      document.removeEventListener('keydown', key);
    };
  }, [open]);

  const go = (to: string) => { setOpen(false); navigate(to); };

  return (
    <div className="user-menu" ref={box}>
      <button className="trigger" aria-haspopup="menu" aria-expanded={open}
              onClick={() => setOpen((o) => !o)}>
        <span className="avatar" aria-hidden />
        USER: {username.toUpperCase()}
        <span className="chev" aria-hidden>▾</span>
      </button>

      {open && (
        <div className="menu" role="menu">
          <button role="menuitem" onClick={() => go('/settings')}>
            Settings
          </button>
          {/* Not a link, because the documentation is in the repository and is
              not served by the app. A menu item that looks live and does
              nothing is worse than one that says why. */}
          <span role="menuitem" aria-disabled="true" className="disabled"
                title="Documentation is not published from the app yet">
            User manual
          </span>
          <hr />
          <button role="menuitem" onClick={() => { setOpen(false); onSignOut(); }}>
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}
