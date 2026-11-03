import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type AnomalyIncident, type AnomalyIncidentAlarm } from '../../api/client';
import { StatusChip } from '../../components/StatusChip';
import { humanise, relativeTime } from '../../lib/format';

/** The open cooling incident a device roots or belongs to, if any.
 *
 *  Prefers the incident this alarm roots, then one this device roots, then
 *  any it is a member of: on a CRAH that is both the root of its hall's
 *  incident and a member of the chiller's, the root view is the useful one. */
export function useDeviceIncident(deviceId: string, alarmId?: string) {
  const list = useQuery({
    queryKey: ['anomaly-incidents', 'device', deviceId],
    queryFn: () => api.anomalyIncidents({ state: 'open', device_id: deviceId, limit: '5' }),
    refetchInterval: 60_000,
    retry: false,
  });
  const items = list.data?.items ?? [];
  const pick: AnomalyIncident | undefined =
    items.find((i) => alarmId && i.root_alarm_id === alarmId)
    ?? items.find((i) => i.root_device_id === deviceId)
    ?? items[0];
  const detail = useQuery({
    queryKey: ['anomaly-incident', pick?.id],
    queryFn: () => api.anomalyIncident(pick!.id),
    enabled: !!pick,
    refetchInterval: 60_000,
    retry: false,
  });
  return { incident: pick, detail: detail.data };
}

const SHOW = 6;

function Lines({ rows, label }: { rows: AnomalyIncidentAlarm[]; label: string }) {
  if (!rows.length) return null;
  return (
    <>
      <p className="incident-tree-label">{label}</p>
      <ul className="incident-tree-list">
        {rows.slice(0, SHOW).map((a) => (
          <li key={a.id}>
            <StatusChip status={a.severity} />
            <Link to={`/alarms/${a.id}`}>{a.device_name}</Link>
            <span className="muted">{humanise(a.alarm_type)} · {relativeTime(a.first_seen)}</span>
          </li>
        ))}
      </ul>
      {rows.length > SHOW && <p className="muted incident-tree-more">and {rows.length - SHOW} more</p>}
    </>
  );
}

/** What the incident holds, by role: the cause, what is folded under it,
 *  and what is kept visible because it is worse than its cause. */
export function IncidentTree({ deviceId, alarmId, heading = true }: {
  deviceId: string; alarmId?: string; heading?: boolean;
}) {
  const { incident, detail } = useDeviceIncident(deviceId, alarmId);
  if (!incident) return null;
  const alarms = detail?.alarms ?? [];
  const isRoot = incident.root_device_id === deviceId;
  return (
    <section className="incident-tree" aria-label="Cooling incident">
      {heading && (
        <h4>{isRoot ? 'Root of a cooling incident' : 'Part of a cooling incident'}</h4>
      )}
      <p className="incident-tree-title">{incident.title}</p>
      {incident.summary && <p className="muted incident-tree-summary">{incident.summary}</p>}
      {incident.tier === 'room' ? (
        // No cause to name: heat with no cooling alarm behind it.
        <Lines label="Heat alarms" rows={alarms} />
      ) : (
        <>
          <Lines label="Cause" rows={alarms.filter((a) => a.role === 'root')} />
          <Lines label="Kept visible: worse than the cause"
                 rows={alarms.filter((a) => a.role === 'member')} />
          <Lines label="Folded under it" rows={alarms.filter((a) => a.role === 'folded')} />
        </>
      )}
      {!!detail?.findings.length && (
        <p className="muted incident-tree-more">
          {detail.findings.length} anomaly {detail.findings.length === 1 ? 'finding' : 'findings'} on
          the same chain.
        </p>
      )}
    </section>
  );
}
