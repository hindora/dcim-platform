import { useQuery } from '@tanstack/react-query';
import { api, type AnomalyIncident } from '../../api/client';
import { relativeTime } from '../../lib/format';

const findingCount = (i: AnomalyIncident) =>
  Array.isArray(i.evidence.finding_ids) ? i.evidence.finding_ids.length : 0;

/** Open cooling incidents, above the alarm list: one cascade, one line.
 *
 *  Renders nothing on a quiet estate - an empty "Incidents: none" box on
 *  every visit would teach people to stop reading it. A device incident
 *  opens its root alarm, whose detail lists what it explains; a room
 *  incident has no root alarm, so it filters the list to the room. */
export function IncidentStrip({ onOpenAlarm, onRoom }: {
  onOpenAlarm: (id: string) => void;
  /** A room's alarms, the folded ones included: one incident, one click. */
  onRoom: (roomId: string) => void;
}) {
  const q = useQuery({
    queryKey: ['anomaly-incidents', 'open'],
    queryFn: () => api.anomalyIncidents({ state: 'open', limit: '20' }),
    refetchInterval: 30_000,
    retry: false,
  });
  const items = q.data?.items ?? [];
  if (!items.length) return null;

  const open = (i: AnomalyIncident) => {
    if (i.root_alarm_id) onOpenAlarm(i.root_alarm_id);
    else if (i.room_id) onRoom(i.room_id);
  };

  return (
    <section className="incident-strip" aria-label="Open cooling incidents">
      <h3>{items.length === 1 ? '1 cooling incident' : `${items.length} cooling incidents`}</h3>
      <ul>
        {items.map((i) => (
          <li key={i.id}>
            <button type="button" className={`incident sev-${(i.evidence.worst ?? 'INFO').toLowerCase()}`}
                    onClick={() => open(i)}>
              <span className="incident-title">{i.title}</span>
              <span className="incident-meta">
                {i.alarm_count} {i.alarm_count === 1 ? 'alarm' : 'alarms'}
                {findingCount(i) ? ` and ${findingCount(i)} ${findingCount(i) === 1
                  ? 'finding' : 'findings'}` : ''}
                {typeof i.evidence.folded === 'number' && i.evidence.folded > 0
                  ? `, ${i.evidence.folded} folded under it` : ''}
                {` · since ${relativeTime(i.opened_at)}`}
              </span>
              {i.summary && <span className="incident-summary">{i.summary}</span>}
            </button>
            {(i.evidence.by_room?.length ?? 0) > 0 && (
              <ul className="incident-rooms" aria-label={`Rooms affected by ${i.title}`}>
                {i.evidence.by_room!.map((r) => (
                  <li key={r.room_id ?? 'none'}>
                    <button type="button" className={`incident-room sev-${r.worst.toLowerCase()}`}
                            disabled={!r.room_id}
                            onClick={() => r.room_id && onRoom(r.room_id)}>
                      <span className="incident-room-name">{r.room_name ?? 'No room'}</span>
                      <span className="incident-room-count">
                        {r.devices} {r.devices === 1 ? 'device' : 'devices'}, {r.alarms} {r.alarms === 1 ? 'alarm' : 'alarms'}
                      </span>
                      <span className="incident-room-sev">worst {r.worst.toLowerCase()}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
