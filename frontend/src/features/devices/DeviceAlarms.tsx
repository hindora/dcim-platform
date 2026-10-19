import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { ApiError, api, atLeast, type Alarm } from '../../api/client';
import { usePaged } from '../../components/Pagination';
import { humanise, relativeTime } from '../../lib/format';

/** The conditions on one device, worst first.
 *
 *  Two shapes. Compact is the overview's "what is wrong right now": the few
 *  worst open conditions, no buttons, a way to the full list. Full is the
 *  Alarms tab, paged like every other list, with acknowledge and clear for
 *  an operator; the caller says whether it is the open conditions or the
 *  whole history. Both read the same query, so acknowledging on the tab
 *  updates the overview.
 *
 *  Acknowledge and clear are the two things a person does from the device
 *  itself: "I have it" and "it is fixed". Clearing by hand asks first - it
 *  reopens if the condition is still there, and the ask says so. */

const SEV_RANK: Record<string, number> = { CRITICAL: 5, MAJOR: 4, MINOR: 3, WARNING: 2, INFO: 1 };

export function sortAlarms(items: Alarm[]): Alarm[] {
  return [...items].sort((a, b) =>
    Number(a.state === 'CLEARED') - Number(b.state === 'CLEARED')
    || (SEV_RANK[b.severity] ?? 0) - (SEV_RANK[a.severity] ?? 0)
    || b.last_seen.localeCompare(a.last_seen));
}

type View = 'open' | 'all';

export function useDeviceAlarms(deviceId: string, view: View = 'open') {
  return useQuery<{ items: Alarm[] }>({
    queryKey: ['alarms', 'device', deviceId, view],
    queryFn: () => api.alarms({
      device_id: deviceId,
      include_symptoms: 'true',
      state: view === 'all' ? ['ACTIVE', 'ACKNOWLEDGED', 'CLEARED'] : undefined,
      limit: '100',
    }),
    refetchInterval: 30_000,
  });
}

function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' });
}

export function DeviceAlarms({ deviceId, deviceName, compact = false, view = 'open', limit, onMore }: {
  deviceId: string;
  deviceName: string;
  compact?: boolean;
  /** Open conditions, or everything ever recorded on the device. */
  view?: View;
  /** Compact only: how many of the worst to show. */
  limit?: number;
  /** Compact only: where "and N more" goes. */
  onMore?: () => void;
}) {
  const qc = useQueryClient();
  const canAct = atLeast('operator');
  const [confirming, setConfirming] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const q = useDeviceAlarms(deviceId, compact ? 'open' : view);

  const refresh = () => {
    for (const key of [['alarms'], ['alarm'], ['alarm-summary'], ['device-state', deviceId],
                       ['twin-scene'], ['estate-alarms'], ['dashboard']]) {
      qc.invalidateQueries({ queryKey: key });
    }
  };
  const fail = (e: unknown) => setError(
    e instanceof ApiError && e.status === 409 ? 'Someone else got there first; the list has been refreshed.'
      : e instanceof ApiError && e.status === 403 ? 'Acknowledging and clearing need the operator role.'
      : `That did not go through: ${String(e instanceof Error ? e.message : e)}`);
  const ack = useMutation({
    mutationFn: (id: string) => api.acknowledgeAlarm(id),
    onSuccess: () => { setError(null); refresh(); },
    onError: (e) => { fail(e); refresh(); },
  });
  const clear = useMutation({
    mutationFn: (id: string) => api.clearAlarm(id),
    onSuccess: () => { setError(null); setConfirming(null); refresh(); },
    onError: (e) => { fail(e); setConfirming(null); refresh(); },
  });

  const all = sortAlarms(q.data?.items ?? []);
  const open = all.filter((a) => a.state !== 'CLEARED').length;
  // One pager, learned once: the full list pages like every other table here,
  // so the sections under it stay where they are however long the history.
  const paged = usePaged(all, { noun: 'conditions' });
  const items = compact ? (limit ? all.slice(0, limit) : all) : paged.rows;
  const hidden = compact ? all.length - items.length : 0;

  return (
    <>
      {!compact && view === 'all' && !q.isLoading && all.length > 0 && (
        <p className="dv-note" style={{ margin: '0 0 10px' }}>
          {all.length} recorded, {open} open. Cleared conditions are dimmed.
        </p>
      )}

      {q.isError && <p className="dv-empty">The alarm list could not be read.</p>}
      {q.isLoading && <p className="dv-empty">Reading…</p>}
      {!q.isLoading && !q.isError && items.length === 0 && (
        <p className="dv-empty">
          {compact || view === 'open' ? `Nothing open on ${deviceName}.` : 'No conditions recorded.'}
        </p>
      )}
      {error && <p className="banner" role="alert">{error}</p>}

      {items.length > 0 && (
        <ul className={`dv-alarms${compact ? ' is-compact' : ''}`}>
          {items.map((a) => {
            const cleared = a.state === 'CLEARED';
            return (
              <li key={a.id}
                  className={`dv-alarm sev-${a.severity.toLowerCase()}${a.is_symptom ? ' is-symptom' : ''}${cleared ? ' is-cleared' : ''}`}>
                <span className="sev">{a.severity.toLowerCase()}</span>
                <div className="body">
                  <Link to={`/alarms/${a.id}`} className="type">{humanise(a.alarm_type)}</Link>
                  {a.instance && <span className="muted"> · {a.instance}</span>}
                  <p className="msg">{a.message}</p>
                  <div className="meta">
                    {cleared ? `Cleared ${a.cleared_at ? relativeTime(a.cleared_at) : ''}`
                      : a.state === 'ACKNOWLEDGED' ? 'Acknowledged' : 'New'}
                    {a.is_symptom ? ' · explained by a root cause' : ''}
                    {` · since ${when(a.first_seen)}`}
                    {a.occurrence_count > 1 ? ` · ${a.occurrence_count} times` : ''}
                  </div>
                </div>
                {!compact && canAct && !cleared && (
                  confirming === a.id ? (
                    <div className="acts" role="group" aria-label="Confirm clear">
                      <span className="ask">Clear by hand? It reopens if the condition is still there.</span>
                      <button type="button" className="danger" disabled={clear.isPending}
                              onClick={() => clear.mutate(a.id)}>
                        {clear.isPending ? 'Clearing…' : 'Clear it'}
                      </button>
                      <button type="button" onClick={() => setConfirming(null)}>Keep it</button>
                    </div>
                  ) : (
                    <div className="acts">
                      {a.state === 'ACTIVE' && (
                        <button type="button"
                                disabled={ack.isPending && ack.variables === a.id}
                                onClick={() => ack.mutate(a.id)}>
                          {ack.isPending && ack.variables === a.id ? 'Acknowledging…' : 'Acknowledge'}
                        </button>
                      )}
                      <button type="button" onClick={() => setConfirming(a.id)}>Clear</button>
                    </div>
                  )
                )}
              </li>
            );
          })}
        </ul>
      )}

      {!compact && paged.foot}
      {compact && hidden > 0 && (
        <p className="dv-note">
          <button type="button" className="linklike" onClick={onMore}>
            and {hidden} more
          </button>
        </p>
      )}
    </>
  );
}
