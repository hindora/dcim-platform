import { useQuery } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';
import { api, type AnomalyIncident } from '../../api/client';
import { relativeTime } from '../../lib/format';
import { IncidentStrip } from '../alarms/IncidentStrip';

const DAY_MS = 24 * 3600 * 1000;

/** The right rail of the home page: what is wrong now, as incidents.
 *
 *  An incident is one cause and everything it explains - the single most
 *  useful thing a home page can show during an event, and the one thing the
 *  alarm counters cannot say on their own. On a quiet day the rail holds the
 *  incidents resolved in the last 24 hours instead, so a shift handover can
 *  read what happened overnight without opening the history. */
export function IncidentRail() {
  const navigate = useNavigate();
  const open = useQuery({
    queryKey: ['anomaly-incidents', 'open'],
    queryFn: () => api.anomalyIncidents({ state: 'open', limit: '20' }),
    refetchInterval: 30_000,
    retry: false,
  });
  const done = useQuery({
    queryKey: ['anomaly-incidents', 'resolved'],
    queryFn: () => api.anomalyIncidents({ state: 'resolved', limit: '20' }),
    refetchInterval: 120_000,
    retry: false,
  });
  const openItems = open.data?.items ?? [];
  const recent: AnomalyIncident[] = (done.data?.items ?? [])
    .filter((i) => i.resolved_at && Date.now() - new Date(i.resolved_at).getTime() < DAY_MS)
    .slice(0, 6);

  return (
    <div className="rail-incidents" aria-label="Incidents">
      <h3>{openItems.length
        ? `${openItems.length} open ${openItems.length === 1 ? 'incident' : 'incidents'}`
        : 'Incidents'}</h3>
      {open.isError && <p className="rail-incident-quiet">Incidents could not be read.</p>}
      {openItems.length > 0 && (
        <IncidentStrip onOpenAlarm={(id) => navigate(`/alarms/${id}`)}
                       onRoom={(room) => navigate(`/alarms?room=${room}&symptoms=1`)} />
      )}
      {!open.isLoading && !open.isError && openItems.length === 0 && (
        <p className="rail-incident-quiet">
          <b>No open incidents.</b> Every open alarm stands on its own; nothing is
          folded under a cause right now.
        </p>
      )}
      {recent.length > 0 && (
        <>
          <h3>Resolved in the last 24 h</h3>
          <ul className="rail-resolved">
            {recent.map((i) => (
              <li key={i.id}>
                <span className="t">{i.title}</span>
                <span className="m">
                  {i.alarm_count} {i.alarm_count === 1 ? 'alarm' : 'alarms'} ·
                  {' '}{relativeTime(i.opened_at)} to {relativeTime(i.resolved_at!)}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
      <p className="rail-links">
        <Link to="/connectivity">Network map</Link>
        <Link to="/floorplan">Floor map</Link>
        <Link to="/world">World map</Link>
      </p>
    </div>
  );
}
