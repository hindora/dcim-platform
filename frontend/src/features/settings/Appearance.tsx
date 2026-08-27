import { useEffect, useState } from 'react';
import {
  type Palette,
  type ThemeMode,
  getMode,
  resolve,
  setMode,
  systemPalette,
  watchSystem,
} from '../../lib/theme';

/** Which palette this browser wears.
 *
 *  Per browser, not per account, and deliberately: a theme is a property of the
 *  screen you are sitting at. The same operator on a wall display in a dim hall
 *  and on a laptop by a window wants different answers, and storing the choice
 *  on the server would make one of those two follow the other. */
export function Appearance() {
  const [mode, setLocalMode] = useState<ThemeMode>(() => getMode());
  const [active, setActive] = useState<Palette>(() => resolve(getMode()));

  // A machine set to switch at dusk is exactly the one nobody reloads, because
  // it is showing a dashboard.
  useEffect(() => watchSystem(setActive), []);

  const choose = (next: ThemeMode) => {
    setLocalMode(next);
    setActive(setMode(next));
  };

  return (
    <div className="stack">
      <div>
        <h2>Appearance</h2>
        <p className="subtitle">
          Applies to this browser only, and takes effect as you choose it.
        </p>
      </div>

      <fieldset className="proto">
        <legend>Theme</legend>
        <div className="form-grid">
          <label>
            <span>Mode</span>
            <select value={mode}
                    onChange={(e) => choose(e.target.value as ThemeMode)}>
              <option value="system">
                Sync with system — currently {systemPalette()}
              </option>
              <option value="light">Light</option>
              <option value="dark">Dark</option>
            </select>
            <em className="hint">
              {mode === 'system'
                ? 'Follows this machine, including when it switches at dusk.'
                : `Stays ${mode} whatever this machine is set to.`}
            </em>
          </label>
        </div>

        <div className="theme-cards">
          <ThemeCard palette="dark" active={active === 'dark'}
                     onPick={() => choose('dark')}
                     note="Built for a dim hall and a wall display." />
          <ThemeCard palette="light" active={active === 'light'}
                     onPick={() => choose('light')}
                     note="For a lit office, and for printing a screen." />
        </div>
      </fieldset>
    </div>
  );
}

/** A card that shows a palette rather than describing it.
 *
 *  Painted with its own literal colours, not with the tokens: a card whose job
 *  is to show you the light theme has to look light while you are sitting in
 *  the dark one. */
function ThemeCard({ palette, active, note, onPick }: {
  palette: Palette;
  active: boolean;
  note: string;
  onPick: () => void;
}) {
  const c = palette === 'dark'
    ? { bg: '#0e1116', raised: '#161b22', line: '#262c36', text: '#e6edf3',
        muted: '#484f58', accent: '#3b82f6', ok: '#2ea043' }
    : { bg: '#ffffff', raised: '#f6f8fa', line: '#d0d7de', text: '#1f2328',
        muted: '#c8d1da', accent: '#0969da', ok: '#1a7f37' };

  return (
    <button type="button" onClick={onPick}
            className={active ? 'theme-card active' : 'theme-card'}
            aria-pressed={active}>
      <span className="head">
        {palette === 'dark' ? 'Dark' : 'Light'}
        {active && <span className="badge">Active</span>}
      </span>

      {/* A miniature of the console: the top bar, the alert strip, a table.
          Enough shape that the two cards differ the way the app does. */}
      <span className="mock" style={{ background: c.bg, borderColor: c.line }}>
        <span className="mock-bar" style={{ background: c.raised,
                                            borderColor: c.line }}>
          <i style={{ background: c.accent }} />
          <i style={{ background: c.muted, width: 26 }} />
          <i style={{ background: c.muted, width: 18 }} />
        </span>
        <span className="mock-body">
          <i style={{ background: c.text, width: 54, height: 7 }} />
          <span className="mock-row" style={{ borderColor: c.line }}>
            <i style={{ background: c.ok, width: 40 }} />
            <i style={{ background: c.muted, width: 22 }} />
          </span>
          <span className="mock-row" style={{ borderColor: c.line }}>
            <i style={{ background: c.muted, width: 34 }} />
            <i style={{ background: c.muted, width: 28 }} />
          </span>
        </span>
      </span>

      <span className="note">{note}</span>
    </button>
  );
}
