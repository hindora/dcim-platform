import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, type Alarm } from '../../../api/client';
import { Seg } from '../../../components/estate';
import { usePaged } from '../../../components/Pagination';
import { downloadCsv, stampedName } from '../../../lib/csv';
import { humanise, relativeTime, statusClass } from '../../../lib/format';
import { Loading, SEVERITY_RANK, useExport, type DrawerCtx } from './shared';

const DAY_MS = 86_400_000;

/** What is wrong with it now, and what was wrong with it today.
 *
 *  Symptoms are included: the alarm page folds them under their root cause so
 *  one failure reads as one incident, but on ONE device's drawer the question
 *  is everything this box is saying, and a symptom is still said by it. They
 *  are marked, not hidden.
 *
 *  Acknowledge lives here so the operator does not have to leave the graph -
 *  and lose their place on it - to take ownership of what they are looking at.
 */
export function Alarms({ open, openLoading, deviceIds, name, roomId, ctx }: {
  /** The open set, fetched by the shell because the tab badge counts it. */
  open: Alarm[];
  openLoading: boolean;
  /** One device, or every member of a rolled-up rack. */
  deviceIds: string[];
  name: string;
  roomId?: string | null;
  ctx: DrawerCtx;
}) {
  const [view, setView] = useState<'open' | 'cleared'>('open');
  const qc = useQueryClient();
  const single = deviceIds.length === 1;

  const cleared = useQuery<{ items: Alarm[] }>({
    queryKey: ['drawer-alarms-cleared', single ? deviceIds[0] : roomId],
    queryFn: () => api.alarms({
      state: 'CLEARED', include_symptoms: 'true', limit: single ? '100' : '500',
      ...(single ? { device_id: deviceIds[0] } : { room: roomId ?? undefined }),
    }),
    enabled: view === 'cleared' && (single || Boolean(roomId)),
  });

  const ack = useMutation({
    mutationFn: (id: string) => api.acknowledgeAlarm(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['drawer-alarms'] });
      qc.invalidateQueries({ queryKey: ['alarms'] });
    },
  });

  // Memoised, not just tidy: the exporter below is keyed on this array, and a
  // fresh array every render re-registers it, which re-renders the shell,
  // which renders this again.
  const items = useMemo(() => {
    if (view === 'open') {
      return [...open].sort((a, b) => (SEVERITY_RANK[a.severity] ?? 5)
        - (SEVERITY_RANK[b.severity] ?? 5) || b.last_seen.localeCompare(a.last_seen));
    }
    const members = new Set(deviceIds);
    const since = Date.now() - DAY_MS;
    return (cleared.data?.items ?? []).filter((a) => members.has(a.device_id)
      && a.cleared_at && new Date(a.cleared_at).getTime() >= since);
  }, [view, open, cleared.data, deviceIds]);
  const loading = view === 'open' ? openLoading : cleared.isLoading;

  const exporter = useCallback(() => downloadCsv(
    stampedName(`alarms-${name}-${view}`),
    ['Device', 'Severity', 'State', 'Type', 'Instance', 'Feeds', 'Message',
     'First seen', 'Last seen', 'Cleared', 'Symptom'],
    items.map((a) => [a.device_name, a.severity, a.state, a.alarm_type, a.instance,
                      a.instance_feeds ?? '', a.message, a.first_seen, a.last_seen,
                      a.cleared_at ?? '', a.is_symptom ? 'yes' : '']),
  ), [items, name, view]);
  useExport(ctx, items.length ? exporter : null);

  const { rows, foot } = usePaged(items, { noun: 'alarms' });

  return (
    <>
      <div className="cd-toolbar">
        <Seg label="Which alarms" value={view} onChange={setView}
             options={[{ key: 'open', label: `Open ${open.length}` },
                       { key: 'cleared', label: 'Cleared 24h' }]} />
        <Link to="/alarms" className="k">All alarms →</Link>
      </div>

      {loading && <Loading />}
      {!loading && items.length === 0 && (
        <p className="muted">
          {view === 'open' ? 'Nothing open on it.' : 'Nothing cleared in the last 24 hours.'}
        </p>
      )}

      <ul className="cd-alarms">
        {rows.map((a) => (
          <li key={a.id} data-tone={statusClass(a.severity)}>
            <div className="cd-alarm-head">
              <span className={`chip ${statusClass(a.severity)}`}>
                <span className="dot" aria-hidden="true" />{a.severity.toLowerCase()}
              </span>
              <span className="k">
                {view === 'open'
                  ? `${relativeTime(a.first_seen)}${a.occurrence_count > 1
                    ? ` · ×${a.occurrence_count}` : ''}`
                  : `cleared ${relativeTime(a.cleared_at)}`}
              </span>
            </div>
            <div className="cd-alarm-msg">{a.message}</div>
            <div className="k">
              {!single && <>{a.device_name} · </>}
              {humanise(a.alarm_type)}
              {a.instance && <> · {a.instance}</>}
              {a.instance_feeds && <> → {a.instance_feeds}</>}
              {a.is_symptom && <> · <i>symptom</i></>}
              {a.state === 'ACKNOWLEDGED' && <> · acknowledged</>}
            </div>
            {a.state === 'ACTIVE' && (
              <button type="button" className="cd-ack"
                      disabled={ack.isPending && ack.variables === a.id}
                      onClick={() => ack.mutate(a.id)}>
                Acknowledge
              </button>
            )}
          </li>
        ))}
      </ul>
      {ack.isError && <p className="warn">Could not acknowledge: {String(ack.error)}</p>}
      {foot}
    </>
  );
}
