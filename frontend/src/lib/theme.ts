/** Which palette the app wears, and who decides.
 *
 *  Three modes, because two is not enough: an operator who has set their
 *  machine to switch at dusk expects this to follow, and one who works in a
 *  lit NOC against a dark hall expects it not to.
 *
 *  Stored per browser rather than per account. A theme is a property of the
 *  screen you are sitting at - the same person on a wall display and on a
 *  laptop wants different answers - and putting it on the server would make
 *  the wall display follow whatever the laptop chose.
 */

export type ThemeMode = 'system' | 'light' | 'dark';
export type Palette = 'light' | 'dark';

const KEY = 'dcim.theme';

/** The default. Dark, because most of these screens live on a wall in a room
 *  kept dim, and because it is what every existing deployment already sees. */
export const DEFAULT_MODE: ThemeMode = 'system';

export function getMode(): ThemeMode {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw === 'light' || raw === 'dark' || raw === 'system') return raw;
  } catch {
    // A browser with storage disabled still gets a working app.
  }
  return DEFAULT_MODE;
}

export function systemPalette(): Palette {
  return window.matchMedia?.('(prefers-color-scheme: light)').matches
    ? 'light' : 'dark';
}

export function resolve(mode: ThemeMode): Palette {
  return mode === 'system' ? systemPalette() : mode;
}

/** Stamp the resolved palette on <html>, which is what the CSS keys off.
 *
 *  Resolved in script rather than left to a media query so that all three
 *  modes go through one path: the CSS has a light block and a dark block, and
 *  nothing has to be written twice to also work under `prefers-color-scheme`.
 */
export function apply(mode: ThemeMode): Palette {
  const palette = resolve(mode);
  document.documentElement.dataset.theme = palette;
  return palette;
}

export function setMode(mode: ThemeMode): Palette {
  try {
    localStorage.setItem(KEY, mode);
  } catch {
    // Not fatal: the choice applies now and is forgotten on reload.
  }
  return apply(mode);
}

/** Follow the system while the mode says to.
 *
 *  Returns an unsubscribe. Without this, "sync with system" would only take
 *  effect on a reload - and the machine that switches at dusk is exactly the
 *  one nobody reloads, because it is showing a dashboard.
 */
export function watchSystem(onChange: (palette: Palette) => void): () => void {
  const media = window.matchMedia?.('(prefers-color-scheme: light)');
  if (!media) return () => {};
  const handler = () => {
    if (getMode() !== 'system') return;
    onChange(apply('system'));
  };
  media.addEventListener('change', handler);
  return () => media.removeEventListener('change', handler);
}
