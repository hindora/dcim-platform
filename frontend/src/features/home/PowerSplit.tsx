/** The site or room power split, as one bar instead of four tiles.
 *
 *  Total, IT, cooling and other were four tiles of identical weight, which is
 *  the wrong shape for the data: they are not four independent measurements,
 *  they are a total and its parts, and total = IT + cooling + other. Reading
 *  the split meant doing the arithmetic yourself every time.
 *
 *  One bar says it at a glance, and it says the thing the tiles could not: how
 *  much of this site is IT and how much is the cost of running it. It also
 *  makes an uninstrumented segment visible AS a gap - "Facility Other 0.0 kW"
 *  read as a measured zero when it is really nothing metered outside the two
 *  named paths.
 */
import type { ReactNode } from 'react';

export interface PowerSegment {
  key: string;
  label: string;
  kw: number | null | undefined;
  /** A theme token name, without the `--`. */
  tone: string;
  /** Shown when the segment is zero, to separate "none" from "not metered". */
  absentNote?: string;
}

function kw(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : v.toFixed(1);
}

export function PowerSplit({ total, segments, caption, note }: {
  total: number | null | undefined;
  segments: PowerSegment[];
  caption: string;
  note?: ReactNode;
}) {
  const known = segments.filter((s) => s.kw != null && s.kw > 0);
  const sum = known.reduce((t, s) => t + (s.kw ?? 0), 0);
  // The bar is drawn against the SEGMENTS' own sum, not the reported total.
  // Where the two genuinely disagree the difference gets its own segment
  // rather than being absorbed silently.
  //
  // "Genuinely" is the epsilon. Every figure here arrives rounded to 0.1 kW,
  // so a residue below half of that is the rounding and nothing else - without
  // the threshold a site whose parts add up exactly still drew a hairline
  // segment and a legend row reading "Unaccounted 0.0 kW", which invites
  // somebody to go looking for a load that does not exist.
  const residue = total != null && sum > 0 ? Math.max(0, total - sum) : 0;
  const unaccounted = residue >= 0.05 ? residue : 0;
  const denom = sum + unaccounted;

  return (
    <div className="power-split">
      <div className="power-total">
        <span className="n">{kw(total)}</span>
        <span className="u">kW</span>
        <span className="cap">{caption}</span>
      </div>

      {denom > 0 ? (
        <>
          <div className="power-bar" role="img"
               aria-label={`${caption}: ${known.map((s) =>
                 `${s.label} ${kw(s.kw)} kW`).join(', ')}`}>
            {known.map((s) => (
              <span key={s.key} className="seg"
                    style={{ width: `${((s.kw ?? 0) / denom) * 100}%`,
                             background: `var(--${s.tone})` }}
                    title={`${s.label} ${kw(s.kw)} kW`} />
            ))}
            {unaccounted > 0 && (
              <span className="seg unaccounted"
                    style={{ width: `${(unaccounted / denom) * 100}%` }}
                    title={`unaccounted ${kw(unaccounted)} kW`} />
            )}
          </div>

          <div className="power-legend">
            {segments.map((s) => {
              const pct = s.kw != null && denom > 0 ? (s.kw / denom) * 100 : null;
              const none = s.kw == null || s.kw === 0;
              return (
                <span key={s.key} className={`leg${none ? ' none' : ''}`}>
                  <i style={{ background: `var(--${s.tone})` }} />
                  <b>{s.label}</b>
                  <span className="v">{kw(s.kw)} kW</span>
                  {pct !== null && !none && (
                    <span className="p">{pct.toFixed(0)}%</span>
                  )}
                  {none && s.absentNote && <span className="p">{s.absentNote}</span>}
                </span>
              );
            })}
            {unaccounted > 0 && (
              <span className="leg">
                <i className="unaccounted" />
                <b>Unaccounted</b>
                <span className="v">{kw(unaccounted)} kW</span>
                <span className="p">
                  metered in the total but not by either path
                </span>
              </span>
            )}
          </div>
        </>
      ) : (
        <p className="muted small">Nothing here reports power.</p>
      )}

      {note && <div className="power-note">{note}</div>}
    </div>
  );
}
