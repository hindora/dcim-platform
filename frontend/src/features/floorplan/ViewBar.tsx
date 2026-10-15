/**
 * The way the picture is looked at, at the top centre: three round camera
 * buttons (turn left, reset, turn right) over a pill of view modes. The
 * round buttons float on their own; the pill is one surface. Turning is a
 * quarter orbit about the room's centre and exists only where there is an
 * orbit - the plan is a map and the walk-through has its own head.
 */

export type Spin = { n: number; dir: 1 | -1 };

function Icon({ kind }: { kind: 'left' | 'right' | 'reset' }) {
  const common = { width: 18, height: 18, viewBox: '0 0 20 20', fill: 'none', stroke: 'currentColor',
                   strokeWidth: 1.6, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const, 'aria-hidden': true };
  switch (kind) {
    case 'left': return <svg {...common}><path d="M4.5 9.5A6 6 0 1 1 6.3 14.2" /><path d="M4 5.5v4h4" /></svg>;
    case 'right': return <svg {...common}><path d="M15.5 9.5A6 6 0 1 0 13.7 14.2" /><path d="M16 5.5v4h-4" /></svg>;
    case 'reset': return (
      <svg {...common}><circle cx="10" cy="10" r="5.5" /><path d="M10 2.5v3M10 14.5v3M2.5 10h3M14.5 10h3" />
        <circle cx="10" cy="10" r="1" fill="currentColor" stroke="none" /></svg>
    );
  }
}

export function ViewBar<T extends string>({ value, options, onChange, canSpin, onSpin, onReset, toggle }: {
  value: T;
  options: { key: T; label: string }[];
  onChange: (v: T) => void;
  /** Whether the current view can be turned (an orbit camera is up). */
  canSpin: boolean;
  onSpin: (dir: 1 | -1) => void;
  onReset: () => void;
  /** An on/off view switch beside the modes (the building's "Split floors"). */
  toggle?: { label: string; title: string; on: boolean; onChange: (on: boolean) => void };
}) {
  return (
    <div className="vw-viewbar">
      <div className="vw-orbit" role="group" aria-label="Camera">
        {canSpin && (
          <button type="button" className="vw-round" aria-label="Turn the view left" title="Turn the view left" onClick={() => onSpin(-1)}>
            <Icon kind="left" />
          </button>
        )}
        <button type="button" className="vw-round" aria-label="Reset the camera" title="Reset the camera" onClick={onReset}>
          <Icon kind="reset" />
        </button>
        {canSpin && (
          <button type="button" className="vw-round" aria-label="Turn the view right" title="Turn the view right" onClick={() => onSpin(1)}>
            <Icon kind="right" />
          </button>
        )}
      </div>
      <div className="vw-viewrow">
        <div className="vw-pill" role="group" aria-label="View">
          {options.map((o) => (
            <button key={o.key} type="button" className={o.key === value ? 'is-on' : undefined}
                    aria-pressed={o.key === value} onClick={() => onChange(o.key)}>
              {o.label}
            </button>
          ))}
        </div>
        {toggle && (
          <div className="vw-pill">
            <button type="button" className={toggle.on ? 'is-on' : undefined} aria-pressed={toggle.on}
                    title={toggle.title} onClick={() => toggle.onChange(!toggle.on)}>
              {toggle.label}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
