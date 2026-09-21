/** What the lines and the bar mean.
 *
 *  Only shown when there is more than one thing to tell apart.
 *
 *  The swatches are solid, because the lines are. A dash on this canvas means
 *  FLOW and belongs to the cooling loop alone. There are no A and B swatches:
 *  every conductor on a layer is drawn in that layer's one colour, and side is
 *  read off the column a feeder sits in on the one-line.
 */
export function Legend({ showLoad, simulating }: {
  showLoad: boolean; simulating?: boolean;
}) {
  if (!showLoad && !simulating) return null;

  const line = (stroke: string, dash?: string) => (
    <svg width="20" height="9" aria-hidden>
      <line x1="0" y1="4.5" x2="20" y2="4.5" strokeWidth="2"
            stroke={stroke} strokeDasharray={dash} />
    </svg>
  );

  return (
    <div className="cn-legend">
      <span>{line('var(--critical)')}Down</span>
      {simulating && (
        <>
          <span><span className="cn-key is-cut" />Goes dark</span>
          <span><span className="cn-key is-partial" />Partly dark</span>
          <span><span className="cn-key is-degraded" />Loses a side</span>
        </>
      )}
      {showLoad && (
        <>
          <span>
            <span className="cn-swatch" />
            Draw against rating
          </span>
          <span>
            {/* The hatch is drawn out rather than referenced from the canvas,
                which would need a document-scoped pattern id that the
                maximized copy would duplicate. */}
            <svg width="20" height="9" aria-hidden>
              <rect x="0" y="2.5" width="20" height="4" rx="2"
                    fill="var(--bg-inset)" stroke="var(--border)" strokeWidth="0.6" />
              {[1, 5, 9, 13, 17].map((x) => (
                <line key={x} x1={x} y1="6.5" x2={x + 4} y2="2.5"
                      stroke="var(--border-strong)" strokeWidth="1" />
              ))}
            </svg>
            No rating recorded
          </span>
        </>
      )}
    </div>
  );
}
