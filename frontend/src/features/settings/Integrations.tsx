import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, api, type Integration, type IntegrationsPage } from '../../api/client';
import { oneLine, relativeTime, untilTime } from '../../lib/format';
import { Tip } from '../../components/HoverTip';
import { IntegrationSheet } from './components/IntegrationSheet';
import { NewIntegration } from './components/NewIntegration';

/** Where alarms become tickets, and what happened to the ones that did.
 *
 *  The page answers three questions in the order an operator asks them:
 *  is it working, what is it about to do, and what did it fail to deliver. The
 *  configuration is behind a button, because it is read once and edited
 *  rarely, whereas "did anything fail" is asked at 03:00.
 */
export function Integrations() {
  const qc = useQueryClient();
  const page = useQuery<IntegrationsPage>({
    queryKey: ['integrations'],
    queryFn: () => api.integrations(),
    refetchInterval: 20_000,
  });

  const [open, setOpen] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const toggle = useMutation({
    mutationFn: (row: Integration) =>
      api.patchIntegration(row.id, { enabled: !row.enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['integrations'] }),
  });

  if (page.isLoading) return <p className="muted">Loading…</p>;
  if (page.error) return <div className="banner">Could not load integrations.</div>;

  const rows = page.data?.items ?? [];
  const publicUrl = page.data?.public_base_url ?? '';

  return (
    <div className="stack">
      <div>
        <h2>Ticketing</h2>
        <p className="subtitle">
          <Tip tip={oneLine(`Not every alarm becomes a ticket. A policy decides
                  which conditions earn one, and a closed ticket acknowledges
                  its alarm rather than clearing it - only the poll clears.`)}>
            Which conditions reach a service desk, and what happened to them.
          </Tip>
        </p>
      </div>

      {!publicUrl && rows.length > 0 && (
        <div className="banner soft">
          <b>DCIM_PUBLIC_BASE_URL is not set.</b> Tickets will be created
          without a link back to the condition that raised them, and no inbound
          webhook can be registered — Jira has no address to call. Set it on
          the backend and restart.
        </div>
      )}

      {toggle.error && (
        <div className="banner">
          {toggle.error instanceof ApiError
            ? toggle.error.message
            : String((toggle.error as Error).message)}
        </div>
      )}

      {rows.length === 0 ? (
        <p className="muted">
          No ticketing integration is configured. Alarms stay on this console,
          which is a perfectly good place for them.
        </p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Integration</th><th>Project</th><th>State</th>
              <th className="num">Open</th><th className="num">Queued</th>
              <th className="num">Failed</th><th>Last delivered</th><th />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td>
                  <div>{row.name}</div>
                  <div className="detail mono">{row.base_url}</div>
                </td>
                <td className="mono">{row.effective.project_key || '—'}</td>
                <td><Health row={row} /></td>
                <td className="num">{row.open_tickets.toLocaleString()}</td>
                <td className="num">{row.pending.toLocaleString()}</td>
                <td className={`num ${row.dead ? 'critical' : ''}`}>
                  {row.dead ? row.dead.toLocaleString() : '—'}
                </td>
                <td className="muted">
                  {row.last_delivered ? relativeTime(row.last_delivered) : 'never'}
                </td>
                <td className="row-actions">
                  <button onClick={() => toggle.mutate(row)}>
                    {row.enabled ? 'Disable' : 'Enable'}
                  </button>
                  <button onClick={() => setOpen(row.id)}>Configure</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="toolbar">
        <button onClick={() => setCreating(true)}>Add an integration</button>
      </div>

      {creating && (
        <NewIntegration onClose={() => setCreating(false)} />
      )}
      {open && page.data && (
        <IntegrationSheet
          row={rows.find((r) => r.id === open)!}
          defaults={page.data.defaults}
          onClose={() => setOpen(null)}
        />
      )}
    </div>
  );
}

/** One word for "is this working", and the reason underneath it.
 *
 *  Deliberately not a green tick. The two ways this fails are both silent -
 *  a credential that lapsed and a queue that gave up - so the states worth
 *  naming are the ones nothing else would mention.
 */
function Health({ row }: { row: Integration }) {
  if (!row.enabled) return <span className="muted">off</span>;

  const days = row.secret_expires_at
    ? (new Date(row.secret_expires_at).getTime() - Date.now()) / 86_400_000
    : null;

  if (days !== null && days < 0) {
    return (
      <Tip className="critical" tip={oneLine(`The stored credential lapsed.
              Nothing has been delivered since, and Atlassian does not warn
              anybody when a token expires.`)}>
        credential expired
      </Tip>
    );
  }
  if (row.dead > 0) {
    return (
      <Tip className="warn" tip={oneLine(`Messages were given up on. Conditions
              that should have raised a ticket did not; the reason is on each
              one below.`)}>
        degraded
      </Tip>
    );
  }
  if (days !== null && days <= 30) {
    return (
      <Tip className="warn"
           tip={`The credential expires ${untilTime(row.secret_expires_at!)}.`}>
        expiring
      </Tip>
    );
  }
  if (!row.webhook_configured) {
    return (
      <Tip className="muted" tip={oneLine(`Outbound only. Tickets are created,
              but closing one will not acknowledge its alarm until a webhook is
              registered.`)}>
        one-way
      </Tip>
    );
  }
  return <span className="muted">running</span>;
}
