/** A utilisation bar that can say "unknown".
 *
 *  The whole reason this is not three lines of inline JSX is the third state.
 *  A constraint whose limit nobody recorded is not at 0% and it is not fine; a
 *  bar drawn empty for it says the opposite of the truth, and it is exactly the
 *  case this fleet is in - no rack, PDU or RPP carries a power rating. So an
 *  unmeasured constraint renders as a hatched track with the usage printed and
 *  no percentage at all, which cannot be misread as headroom.
 */

const TIGHT_PCT = 80;
const FULL_PCT = 95;

function toneFor(pct: number): string {
  if (pct >= FULL_PCT) return 'critical';
  if (pct >= TIGHT_PCT) return 'warn';
  return 'ok';
}

export function Meter({ label, used, capacity, unit, note, binding = false }: {
  label: string;
  used: number | null;
  capacity: number | null;
  unit: string;
  note?: string | null;
  /** The constraint that runs out first, called out rather than left to the eye. */
  binding?: boolean;
}) {
  const known = capacity !== null && capacity > 0;
  const pct = known && used !== null ? (used / capacity!) * 100 : null;

  return (
    <div className={`meter${binding ? ' binding' : ''}`}>
      <div className="meter-head">
        <span className="meter-label">
          {label}
          {binding && <span className="pill">binds first</span>}
        </span>
        <span className="meter-value">
          {used === null ? '—' : `${used.toFixed(used < 10 ? 2 : 1)} ${unit}`}
          {known && <span className="muted"> / {capacity!.toFixed(0)} {unit}</span>}
        </span>
      </div>

      <div className={`meter-track${known ? '' : ' unknown'}`}>
        {pct !== null && (
          <div className={`meter-fill ${toneFor(pct)}`}
               style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
        )}
      </div>

      <div className="meter-foot">
        {pct === null
          ? <span className="muted">no limit recorded — usage is known, headroom is not</span>
          : <span className={toneFor(pct)}>{pct.toFixed(1)}% used</span>}
        {note && <span className="muted meter-note">{note}</span>}
      </div>
    </div>
  );
}
