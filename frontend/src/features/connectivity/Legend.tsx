import { useChartColors } from '../../components/seriesColors';

/** What the lines and the bar mean.
 *
 *  Only shown when there is more than one thing to tell apart. The A/B
 *  swatches carry their pattern as well as their hue, because that is the
 *  distinction the diagram is making and hue is its weakest channel.
 */
export function Legend({ sides, showLoad, simulating }: {
  sides: string[]; showLoad: boolean; simulating?: boolean;
}) {
  const colors = useChartColors();
  if (sides.length < 2 && !showLoad && !simulating) return null;

  const line = (stroke: string, dash?: string) => (
    <svg width="20" height="9" aria-hidden>
      <line x1="0" y1="4.5" x2="20" y2="4.5" strokeWidth="2"
            stroke={stroke} strokeDasharray={dash} />
    </svg>
  );

  return (
    <div className="cn-legend">
      {sides.length > 1 && sides.map((s) => (
        <span key={s}>
          {s === 'A' ? line(colors.series[0]) : line(colors.series[1], '10 4 2 4')}
          Side {s}
        </span>
      ))}
      <span>{line('var(--critical)', '5 3')}Down</span>
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
