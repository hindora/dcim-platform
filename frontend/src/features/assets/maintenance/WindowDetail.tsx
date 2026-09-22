import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { ApiError, api, type MaintenanceWindow } from '../../../api/client';
import { usePaged } from '../../../components/Pagination';
import { humanise, oneLine, relativeTime, untilTime } from '../../../lib/format';
import { Tip } from '../../../components/HoverTip';

/** One window: what it covers, and what it is holding back.
 *
 *  The shelved list is the reason this page exists. "Did anything ELSE break
 *  while we were in there" is asked after every window, and it can only be
 *  answered because the alarms were raised and stored rather than suppressed at
 *  source.
 */
export function WindowDetail() {
  const { id = '' } = useParams();
  const qc = useQueryClient();
  const [actionError, setActionError] = useState<string | null>(null);

  const act = useMutation({
    mutationFn: (action: 'start' | 'complete' | 'cancel') =>
      api.windowAction(id, action),
    onSuccess: () => {
      setActionError(null);
      qc.invalidateQueries({ queryKey: ['maintenance-window', id] });
      qc.invalidateQueries({ queryKey: ['maintenance-windows'] });
      // Shelving moved, so every roll-up the console draws may have changed.
      qc.invalidateQueries({ queryKey: ['alarms'] });
      qc.invalidateQueries({ queryKey: ['asset-devices'] });
    },
    onError: (e) => setActionError(e instanceof ApiError ? e.message : String(e)),
  });

  const askChange = useMutation({
    mutationFn: () => api.requestChange(id),
    onSuccess: () => {
      setActionError(null);
      qc.invalidateQueries({ queryKey: ['maintenance-window', id] });
    },
    onError: (e) => setActionError(e instanceof ApiError ? e.message : String(e)),
  });

  const { data, isLoading, error } = useQuery<MaintenanceWindow>({
    queryKey: ['maintenance-window', id],
    queryFn: () => api.maintenanceWindow(id),
    enabled: Boolean(id),
    refetchInterval: 30_000,
  });
  const pagedTargets = usePaged(data?.targets ?? [], { noun: 'devices' });
  const pagedShelved = usePaged(data?.shelved ?? [], { noun: 'alarms' });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (isLoading || !data) return <p className="muted">Loading…</p>;

  return (
    <>
      <p className="asset-table-note">
        <Link to="/assets/maintenance">← Maintenance</Link>
      </p>

      <div className="asset-record-head">
        <h2>{data.title}</h2>
        <span className={`asset-life is-${data.status}`}>{humanise(data.status)}</span>
      </div>

      {data.blocked_reason && (
        <div className="banner">
          <b>This window has not opened:</b> {data.blocked_reason}. Its start
          time has passed; nothing is shelved and nothing is being worked on.
        </div>
      )}

      <p className="asset-table-note">
        {data.status === 'scheduled' && (
          <Tip tip={data.require_approval
                    && data.jira_approval_state !== 'approved'
                    ? oneLine(`Held: this window requires approval and its
                        change request has not been approved. The gate applies
                        to this button as well as to the clock.`)
                    : undefined}>
            <button type="button"
                    disabled={act.isPending
                              || (Boolean(data.require_approval)
                                  && data.jira_approval_state !== 'approved')}
                    onClick={() => act.mutate('start')}>
              Start now
            </button>
          </Tip>
        )}
        {data.status === 'scheduled' && !data.jira_issue_key
          && !data.jira_approval_state && !askChange.isSuccess && (
          <Tip tip={oneLine(`The change request carries what the window
                  actually costs - how many alarms it silences, how many
                  machines it darkens, which redundant side it removes. That
                  is the number a change advisory board needs and the one only
                  a DCIM can compute.`)}>
            <button type="button" disabled={askChange.isPending}
                    onClick={() => askChange.mutate()}>
              {askChange.isPending ? 'Requesting…' : 'Request change'}
            </button>
          </Tip>
        )}
        {data.status === 'active' && (
          <button type="button" disabled={act.isPending}
                  onClick={() => act.mutate('complete')}>
            End window
          </button>
        )}
        {(data.status === 'scheduled' || data.status === 'active') && (
          <button type="button" disabled={act.isPending}
                  onClick={() => act.mutate('cancel')}>
            Cancel
          </button>
        )}
      </p>

      {actionError && <div className="banner">{actionError}</div>}

      {data.change_ref && data.jira_issue_key
        && data.change_ref !== data.jira_issue_key && (
        <div className="banner soft">
          Two change references disagree: somebody typed{' '}
          <b>{data.change_ref}</b>, and this platform opened{' '}
          <b>{data.jira_issue_key}</b>. The approval gate follows the second.
        </div>
      )}

      <div className="asset-facts" style={{ marginBottom: 20 }}>
        <div className="asset-fact">
          <div className="k">Kind</div><div className="v">{humanise(data.kind)}</div>
        </div>
        <div className="asset-fact">
          <div className="k">Starts</div>
          <div className="v" title={data.starts_at}>{untilTime(data.starts_at)}</div>
        </div>
        <div className="asset-fact">
          <div className="k">Ends</div>
          <div className="v" title={data.ends_at}>{untilTime(data.ends_at)}</div>
        </div>
        <div className="asset-fact">
          <div className="k">Change reference</div>
          <div className="v asset-tag">
            {data.change_ref ?? <span className="asset-none">—</span>}
          </div>
        </div>
        <div className="asset-fact">
          <div className="k">Change request</div>
          <div className="v asset-tag">
            {data.jira_issue_key ?? (
              askChange.isSuccess
                ? <span className="asset-none">queued…</span>
                : <span className="asset-none">—</span>
            )}
          </div>
        </div>
        <div className="asset-fact">
          <div className="k">Approval</div>
          <div className="v"><Approval window={data} /></div>
        </div>
        <div className="asset-fact">
          <div className="k">Scheduled by</div><div className="v">{data.created_by}</div>
        </div>
        <div className="asset-fact">
          <div className="k">Suppressing</div>
          <div className="v">{data.suppress ? 'Yes' : 'No — calendar only'}</div>
        </div>
      </div>

      {data.description && <p className="muted">{data.description}</p>}

      <h3>Devices — {data.targets?.length ?? 0}</h3>
      {data.targets && data.targets.length > 0 ? (
        <div className="asset-scroll">
          <table>
            {/* "What would this actually take out" is asked HERE, in front of
                the window, not on a topology page somebody has to go and find.
                The link lands on the connectivity diagram with the removal
                already simulated and the room already scoped. */}
            <thead><tr><th>Device</th><th>Type</th><th>Severity</th><th /></tr></thead>
            <tbody>
              {pagedTargets.rows.map((t) => (
                <tr key={t.id}>
                  <td><Link to={`/assets/inventory/${t.id}`}>{t.name}</Link></td>
                  <td className="muted">{humanise(t.device_type)}</td>
                  <td className="muted">{t.max_severity}</td>
                  <td>
                    <Link to={`/connectivity?simulate=${t.id}&layer=power`}>
                      What this takes out →
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">This window covers no devices, so it silences nothing.</p>
      )}
      {pagedTargets.foot}

      <h3 style={{ marginTop: 24 }}>
        Shelved alarms — {data.shelved?.length ?? 0}
      </h3>
      {data.shelved && data.shelved.length > 0 ? (
        <div className="asset-scroll">
          <table>
            <thead>
              <tr>
                <th>Device</th><th>Alarm</th><th>Severity</th>
                <th>State</th><th>Since</th>
              </tr>
            </thead>
            <tbody>
              {pagedShelved.rows.map((a) => (
                <tr key={a.id}>
                  <td><Link to={`/assets/inventory/${a.device_id}`}>{a.device_name}</Link></td>
                  <td>{humanise(a.alarm_type)}</td>
                  <td className="muted">{a.severity}</td>
                  <td className="muted">{humanise(a.state)}</td>
                  <td className="muted" title={a.first_seen}>
                    {relativeTime(a.first_seen)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">Nothing shelved.</p>
      )}
      {pagedShelved.foot}
    </>
  );
}


/** Whether anybody is waiting on a decision, and what it was.
 *
 *  NULL is not "not yet approved". A window that never asked for approval has
 *  no approval state, and showing both the same way would make every window
 *  look like it was stuck behind somebody.
 */
function Approval({ window }: { window: MaintenanceWindow }) {
  const state = window.jira_approval_state;
  if (!state) {
    return window.require_approval
      ? <span className="warn">required, not yet requested</span>
      : <span className="asset-none">not required</span>;
  }
  if (state === 'approved') return <span className="muted">approved</span>;
  if (state === 'declined') {
    return (
      <Tip className="critical" tip={oneLine(`A declined change cancels its
              window: the work is not happening, and a window left scheduled
              would open at 02:00 and shelve alarms on equipment nobody is
              touching.`)}>
        declined
      </Tip>
    );
  }
  return (
    <Tip className="warn" tip={oneLine(`The window will not start until the
            change request is approved - by the clock or by the button.`)}>
      awaiting approval
    </Tip>
  );
}
