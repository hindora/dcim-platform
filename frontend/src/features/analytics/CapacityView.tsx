import { useQuery } from '@tanstack/react-query';
import { api, type CapacityReport, type RoomSummary } from '../../api/client';
import { Meter } from '../../components/Meter';

/** Four constraints at once, because the useful answer is not "this room is at
 *  40% power" - it is "this room runs out of SPACE first, and no amount of
 *  power headroom changes that".
 *
 *  The binding constraint is stated in words above the bars rather than left
 *  for the reader to spot by comparing four percentages, and constraints with
 *  no recorded limit are drawn as unmeasured rather than as empty.
 */

const SOURCE_WORDS: Record<string, string> = {
  measured: 'measured from the plant',
  derived: 'derived',
  inferred: 'inferred from operational state',
  assumed: 'assumed, not recorded in inventory',
  unknown: 'no rating recorded',
};

export function CapacityView({ room }: { room: RoomSummary }) {
  const { data, error, isLoading } = useQuery<CapacityReport>({
    queryKey: ['capacity', room.id],
    queryFn: () => api.capacity('room', room.id),
    staleTime: 60_000,
  });

  if (isLoading) return <p className="muted">Loading…</p>;
  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (!data) return null;

  const unmeasured = data.constraints.filter((c) => !c.capacity);

  return (
    <>
      <div className="verdict">
        <div className="verdict-label">Binding constraint</div>
        <div className="verdict-value">
          {data.binding_constraint ?? 'none can be judged'}
        </div>
        <p className="verdict-reason">{data.binding_reason}</p>
      </div>

      <p className="muted small">
        Usage is the {data.percentile}th percentile of the coincident load over{' '}
        {data.window_hours.toFixed(1)} h — the percentile of the summed load, not
        the sum of each device's percentile, which would assume everything peaks
        in the same minute.
      </p>

      <div className="meters">
        {data.constraints.map((c) => (
          <Meter key={c.name}
                 label={c.name}
                 used={c.used_p95}
                 capacity={c.capacity}
                 unit={c.unit}
                 binding={c.name === data.binding_constraint}
                 note={`${SOURCE_WORDS[c.capacity_source] ?? c.capacity_source}${
                   c.note ? ` — ${c.note}` : ''}`} />
        ))}
      </div>

      {unmeasured.length > 0 && (
        <div className="banner soft">
          {unmeasured.map((c) => c.name).join(', ')} could not be judged: no
          rating is recorded, so something else may run out first. That is an
          inventory gap, not a clean bill of health.
        </div>
      )}

      {data.notes.map((n) => (
        <p key={n} className="muted small">{n}</p>
      ))}
    </>
  );
}
