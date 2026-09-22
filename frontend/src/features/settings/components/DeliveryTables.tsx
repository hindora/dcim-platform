import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type InboundRow, type OutboxRow, type TicketLink } from '../../../api/client';
import { Pagination } from '../../../components/Pagination';
import { Seg } from '../../../components/estate';
import { Tip } from '../../../components/HoverTip';
import { oneLine, relativeTime } from '../../../lib/format';

const STATES = ['dead', 'pending', 'done'] as const;

/** What is queued outbound, and what was given up on.
 *
 *  Defaults to the failures. Everything else is noise on this tab: a healthy
 *  queue is empty within seconds and nobody opens a settings page to watch it
 *  drain, whereas a dead letter is a condition somebody should have been told
 *  about and was not.
 */
export function OutboxTable({ integrationId }: { integrationId: string }) {
  const qc = useQueryClient();
  const [state, setState] = useState<string>('dead');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);

  const rows = useQuery<{ items: OutboxRow[] }>({
    queryKey: ['integration-outbox', integrationId, state, page, pageSize],
    queryFn: () => api.integrationOutbox(integrationId, {
      state, limit: pageSize + 1, offset: (page - 1) * pageSize,
    }),
    refetchInterval: 15_000,
  });

  const retry = useMutation({
    mutationFn: (row: number) => api.retryOutbox(integrationId, row),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['integration-outbox'] });
      qc.invalidateQueries({ queryKey: ['integrations'] });
    },
  });

  const all = rows.data?.items ?? [];
  const shown = all.slice(0, pageSize);

  return (
    <div className="stack">
      <div className="asset-panel-filters">
        <Seg value={state} label="Delivery state"
             onChange={(v) => { setState(v); setPage(1); }}
             options={STATES.map((s) => ({ key: s, label: s.toUpperCase() }))} />
      </div>

      {retry.error && (
        <div className="banner">{String((retry.error as Error).message)}</div>
      )}

      {shown.length === 0 ? (
        <p className="muted">
          {state === 'dead'
            ? 'Nothing has been given up on.'
            : 'Nothing here.'}
        </p>
      ) : (
        <>
          <table>
            <thead>
              <tr>
                <th>Condition</th><th>Action</th>
                <th className="num">Tries</th><th>When</th>
                <th>Why it stopped</th><th />
              </tr>
            </thead>
            <tbody>
              {shown.map((row) => (
                <tr key={row.id}>
                  <td>
                    <div>{row.device_name ?? 'platform'}</div>
                    <div className="detail">{row.message ?? ''}</div>
                  </td>
                  <td className="muted">{row.kind.replace('alarm_', '')}</td>
                  <td className="num">{row.attempts}</td>
                  <td className="muted">{relativeTime(row.created_at)}</td>
                  <td className="detail">{row.last_error ?? '—'}</td>
                  <td>
                    {row.state === 'dead' && (
                      <Tip tip={oneLine(`Puts it back in the queue with its
                              attempts reset. Do this after fixing whatever the
                              error names - a missing custom field, a project
                              key - not before.`)}>
                        <button onClick={() => retry.mutate(row.id)}>Retry</button>
                      </Tip>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination page={page} pageSize={pageSize} shown={shown.length}
                      hasNext={all.length > pageSize} noun="messages"
                      onPage={setPage}
                      onSize={(n) => { setPageSize(n); setPage(1); }} />
        </>
      )}
    </div>
  );
}

/** What Jira has told us, in the order it told us.
 *
 *  Kept beside the outbox because the question an operator actually has is
 *  "did the loop close", and half an answer is on each side.
 */
export function InboxTable({ integrationId }: { integrationId: string }) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);

  const rows = useQuery<{ items: InboundRow[] }>({
    queryKey: ['integration-inbox', integrationId, page, pageSize],
    queryFn: () => api.integrationInbox(integrationId, {
      limit: pageSize + 1, offset: (page - 1) * pageSize,
    }),
    refetchInterval: 15_000,
  });

  const all = rows.data?.items ?? [];
  const shown = all.slice(0, pageSize);

  if (shown.length === 0) {
    return (
      <p className="muted">
        Nothing inbound yet. Jira only calls once a webhook is registered, and
        only about tickets this platform opened.
      </p>
    );
  }

  return (
    <div className="stack">
      <table>
        <thead>
          <tr>
            <th>Issue</th><th>Event</th><th>State</th>
            <th>Received</th><th>Error</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((row) => (
            <tr key={row.id}>
              <td className="mono">{row.issue_key ?? '—'}</td>
              <td className="muted">{row.event.replace('jira:', '')}</td>
              <td className={row.state === 'dead' ? 'critical' : 'muted'}>
                {row.state}
              </td>
              <td className="muted">{relativeTime(row.received_at)}</td>
              <td className="detail">{row.last_error ?? ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Pagination page={page} pageSize={pageSize} shown={shown.length}
                  hasNext={all.length > pageSize} noun="events"
                  onPage={setPage}
                  onSize={(n) => { setPageSize(n); setPage(1); }} />
    </div>
  );
}

/** Conditions that have a ticket, and where that ticket got to. */
export function LinksTable({ integrationId }: { integrationId: string }) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);

  const rows = useQuery<{ items: TicketLink[] }>({
    queryKey: ['integration-links', integrationId, page, pageSize],
    queryFn: () => api.integrationLinks(integrationId, {
      limit: pageSize + 1, offset: (page - 1) * pageSize,
    }),
  });

  const all = rows.data?.items ?? [];
  const shown = all.slice(0, pageSize);

  if (shown.length === 0) {
    return <p className="muted">No condition has a ticket yet.</p>;
  }

  return (
    <div className="stack">
      <table>
        <thead>
          <tr>
            <th>Issue</th><th>Condition</th><th>Device</th>
            <th>Ticket</th><th>Alarm</th>
            <th className="num">Pushes</th><th>Opened</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((row) => (
            <tr key={row.fingerprint}>
              <td>
                <a href={row.url} target="_blank" rel="noreferrer"
                   className="mono">{row.issue_key}</a>
              </td>
              <td>{row.alarm_type ?? '—'}</td>
              <td className="muted">{row.device_name ?? '—'}</td>
              <td className={row.closed_at ? 'muted' : ''}>
                {row.wont_reopen ? (
                  <Tip className="warn" tip={oneLine(`Resolved as a decision
                          rather than a fix. A recurrence will open a fresh
                          ticket rather than reopening this one.`)}>
                    {row.resolution ?? 'declined'}
                  </Tip>
                ) : (row.status ?? (row.closed_at ? 'closed' : 'open'))}
              </td>
              <td className="muted">{row.state?.toLowerCase() ?? 'gone'}</td>
              <td className="num">{row.push_count}</td>
              <td className="muted">{relativeTime(row.opened_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Pagination page={page} pageSize={pageSize} shown={shown.length}
                  hasNext={all.length > pageSize} noun="tickets"
                  onPage={setPage}
                  onSize={(n) => { setPageSize(n); setPage(1); }} />
    </div>
  );
}
