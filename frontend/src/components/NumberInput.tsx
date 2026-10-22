import { useEffect, useRef, useState, type CSSProperties } from 'react';

/** A controlled numeric input that behaves for people.
 *
 *  Ported from the simulator web UI (`webui/src/components/NumberInput.tsx`,
 *  doc 31 D1), same props and same behaviour. The naive
 *  `value={n} onChange={parseInt(e.target.value) || fallback}` pattern forces
 *  the box to a number the instant it is cleared, so it can never be empty
 *  mid-edit, and typing after a clear prepends the forced value (clear 15000,
 *  type 8, get "08000"). This keeps a local STRING so the box may be empty
 *  while someone types, emits the parsed number to the parent as they go, and
 *  coerces an empty or invalid box to `fallback` only on blur.
 *
 *  Additive over the original: `id`, `name` and `aria-*`, so a
 *  `<label htmlFor>` and an inline error (`aria-describedby`) can attach to
 *  it. Range is NOT clamped here: an out-of-range value is the caller's to
 *  report beside the field, because silently snapping 12 to 10 hides the
 *  mistake instead of explaining it.
 */
export interface NumberInputProps {
  value: number;
  onChange: (n: number) => void;
  /** Applied when the box is left empty or invalid on blur. */
  fallback?: number;
  /** Parse with parseInt (counts, seconds) rather than parseFloat. */
  int?: boolean;
  min?: number;
  max?: number;
  step?: number;
  style?: CSSProperties;
  className?: string;
  placeholder?: string;
  title?: string;
  disabled?: boolean;
  onFocus?: () => void;
  /** Fired after the internal empty-to-fallback coerce. */
  onBlur?: () => void;
  id?: string;
  name?: string;
  'aria-label'?: string;
  'aria-invalid'?: boolean;
  'aria-describedby'?: string;
}

export function NumberInput({
  value, onChange, fallback = 0, int = false,
  min, max, step, style, className, placeholder, title, disabled,
  onFocus, onBlur, id, name,
  'aria-label': ariaLabel, 'aria-invalid': ariaInvalid,
  'aria-describedby': ariaDescribedBy,
}: NumberInputProps) {
  const [str, setStr] = useState<string>(String(value));
  // The last number WE emitted, so a parent re-render caused by our own
  // onChange does not clobber the string being typed - but an external
  // change (a refetch, a Revert) still syncs the box.
  const last = useRef<number>(value);
  useEffect(() => {
    if (value !== last.current) {
      setStr(String(value));
      last.current = value;
    }
  }, [value]);

  const handle = (raw: string) => {
    setStr(raw);
    if (raw === '' || raw === '-') return;          // empty or partial mid-edit
    const n = int ? parseInt(raw, 10) : parseFloat(raw);
    if (!Number.isNaN(n)) {
      last.current = n;
      onChange(n);
    }
  };
  const blur = () => {
    const n = int ? parseInt(str, 10) : parseFloat(str);
    if (str === '' || Number.isNaN(n)) {
      setStr(String(fallback));
      last.current = fallback;
      onChange(fallback);
    } else if (String(n) !== str) {
      setStr(String(n));                            // normalise "08" to "8"
    }
    onBlur?.();
  };

  return (
    <input
      type="number"
      value={str}
      id={id} name={name}
      min={min} max={max} step={step}
      placeholder={placeholder} title={title} disabled={disabled}
      style={style} className={className}
      aria-label={ariaLabel} aria-invalid={ariaInvalid || undefined}
      aria-describedby={ariaDescribedBy}
      onChange={(e) => handle(e.target.value)}
      onFocus={onFocus}
      onBlur={blur}
    />
  );
}

export default NumberInput;
