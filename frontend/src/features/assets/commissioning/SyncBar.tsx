import { useEffect, useRef } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type SyncStatus } from '../../../api/client';
import { relativeTime } from '../../../lib/format';

/** Read the estate from the device plane, from the UI.
 *
 *  Without this the commissioning path has a hole in the middle: a device
 *  somebody racked is invisible here until an engineer runs the importer from a
 *  shell, which is not something the operator this page was built for can do.
 *
 *  Polls only while a run is going. The import takes about ninety seconds, so a
 *  fixed interval would either be too slow to feel responsive or would hammer the
 *  API for the 23 hours a day nothing is syncing.
 */
export function SyncBar() {
  const qc = useQueryClient();
  const { data } = useQuery<SyncStatus>({
    queryKey: ['sync-status'],
    queryFn: api.syncStatus,
    refetchInterval: (q) => (q.state.data?.running ? 3_000 : false),
  });

  const start = useMutation({
    mutationFn: api.startSync,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['sync-status'] }),
  });

  const running = data?.running;
  const last = data?.recent?.find((r) => r.status !== 'running');

  // The queue is what a finished sync changes, so refetch it when one ends
  // rather than making the operator reload the page to see what arrived.
  //
  // In an effect, not in render: invalidating a query is a side effect, and doing
  // it while rendering can re-enter the render it was called from.
  const wasRunning = usePrevious(Boolean(running));
  useEffect(() => {
    if (wasRunning && !running) {
      qc.invalidateQueries({ queryKey: ['commissioning-queue'] });
      qc.invalidateQueries({ queryKey: ['asset-summary'] });
    }
  }, [wasRunning, running, qc]);

  return (
    <div className="asset-toolbar">
      <button type="button"
              disabled={Boolean(running) || start.isPending
                        || data?.configured === false}
              onClick={() => start.mutate()}
              title={data?.not_configured_reason
                     ?? 'Read the estate from the device plane'}>
        {running ? 'Syncing…' : 'Sync from device plane'}
      </button>

      {running && (
        <span className="muted">
          started {relativeTime(running.started_at)} by {running.actor}
        </span>
      )}

      {!running && last && (
        <span className="muted">
          Last sync {relativeTime(last.started_at)}
          {last.status === 'failed'
            ? ' — failed'
            : `${devicesOf(last.report)} · ${last.seconds ?? '?'}s`}
        </span>
      )}

      {!running && !last && <span className="muted">Never synced.</span>}

      {/* A 503 says which setting is unset. Showing it beats a disabled button
          with no explanation, which reads as a broken page. */}
      {data?.configured === false && (
        <div className="banner">{data.not_configured_reason}</div>
      )}
      {start.error && <div className="banner">{String(start.error)}</div>}
      {last?.status === 'failed' && last.error && (
        <div className="banner">Last sync failed: {last.error}</div>
      )}
    </div>
  );
}

/** "· 641 devices, 3 decommissioned" from whatever the importer reported. Reads
 *  the report defensively: its shape is the importer's to change. */
function devicesOf(report?: Record<string, unknown> | null): string {
  if (!report) return '';
  const n = Number(report.devices ?? NaN);
  const gone = Number(report.decommissioned ?? 0);
  const back = Number(report.resurrected ?? 0);
  const bits: string[] = [];
  if (Number.isFinite(n)) bits.push(`${n} devices`);
  if (gone) bits.push(`${gone} decommissioned`);
  if (back) bits.push(`${back} back`);
  return bits.length ? ` — ${bits.join(', ')}` : '';
}

/** The value from the previous render. Used to notice a run FINISHING, which is
 *  the moment the queue below is stale. */
function usePrevious<T>(value: T): T | undefined {
  const ref = useRef<T>();
  useEffect(() => { ref.current = value; }, [value]);
  return ref.current;
}
