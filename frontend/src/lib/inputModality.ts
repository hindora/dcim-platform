/**
 * Which hand is driving: the pointer or the keyboard. Written to
 * `<html data-input>` so CSS can keep focus rings for the keyboard and hide
 * them from the mouse. Browsers already do this for buttons through
 * :focus-visible, but not for selects, date boxes and sliders, which ring on
 * a click too - and the ring reads as a stray blue border on a console.
 *
 * A pointer press says "pointer"; any key says "keyboard". Modifier keys on
 * their own do not count, so Ctrl+click stays a click.
 */
const MODIFIERS = new Set(['Shift', 'Control', 'Alt', 'Meta']);

export function watchInputModality(): () => void {
  const root = document.documentElement;
  const pointer = () => { root.dataset.input = 'pointer'; };
  const keyboard = (e: KeyboardEvent) => { if (!MODIFIERS.has(e.key)) root.dataset.input = 'keyboard'; };
  root.dataset.input = 'pointer';
  window.addEventListener('pointerdown', pointer, { capture: true, passive: true });
  window.addEventListener('keydown', keyboard, { capture: true });
  return () => {
    window.removeEventListener('pointerdown', pointer, { capture: true });
    window.removeEventListener('keydown', keyboard, { capture: true });
  };
}
