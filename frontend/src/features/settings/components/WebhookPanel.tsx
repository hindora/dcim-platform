import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api, type Integration, type WebhookProvision } from '../../../api/client';
import { Tip } from '../../../components/HoverTip';
import { oneLine, untilTime } from '../../../lib/format';

/** The inbound half: how a closed ticket reaches the alarm it was about.
 *
 *  This panel exists because the honest answer is not a button that works.
 *  Jira Cloud restricts the webhook REST API to Connect and OAuth apps, so a
 *  service account using an API token — which is what this product uses —
 *  gets a 403 and an administrator has to register it by hand. Hiding that
 *  behind a spinner that fails would be worse than saying so and handing over
 *  exactly what to paste.
 */
export function WebhookPanel({ row }: { row: Integration }) {
  const qc = useQueryClient();
  const [result, setResult] = useState<WebhookProvision | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  const provision = useMutation<WebhookProvision>({
    mutationFn: () => api.registerWebhook(row.id),
    onSuccess: (data) => {
      setResult(data);
      qc.invalidateQueries({ queryKey: ['integrations'] });
    },
  });

  function copy(what: string, value: string) {
    navigator.clipboard?.writeText(value).then(
      () => { setCopied(what); setTimeout(() => setCopied(null), 2000); },
      () => setCopied(null),
    );
  }

  return (
    <div className="stack">
      <p className="muted">
        <Tip tip={oneLine(`Without it, tickets are created but closing one does
                nothing here: the alarm stays on the console until the poll
                clears it, and nobody is told the two disagree.`)}>
          Registering a webhook is what lets a closed ticket acknowledge its
          alarm.
        </Tip>
      </p>

      {row.webhook_configured ? (
        <dl className="kv">
          <dt>State</dt>
          <dd>
            registered
            {row.webhook_id && <span className="muted"> · automatically</span>}
          </dd>
          <dt>Expires</dt>
          <dd>
            {row.webhook_expires_at ? (
              <Tip className="warn" tip={oneLine(`A registration made through
                      Jira's REST API lapses after 30 days. This platform
                      renews it a week ahead and alarms if it cannot.`)}>
                {untilTime(row.webhook_expires_at)}
              </Tip>
            ) : (
              <span className="muted">
                never — a hand-registered webhook has no deadline
              </span>
            )}
          </dd>
        </dl>
      ) : (
        <div className="banner soft">
          No webhook. This integration is outbound only: it will open and
          update tickets, and it will never hear back.
        </div>
      )}

      <div className="toolbar">
        <button onClick={() => provision.mutate()} disabled={provision.isPending}>
          {row.webhook_configured ? 'Rotate the secret' : 'Set up the webhook'}
        </button>
        {row.webhook_configured && (
          <span className="muted">
            rotating retires the current secret immediately — Jira has to be
            updated with the new one
          </span>
        )}
      </div>

      {provision.error && (
        <div className="banner">{String((provision.error as Error).message)}</div>
      )}

      {result && (
        <section className="asset-panel">
          <h3>Give these to Jira</h3>

          <p className="muted">{result.instructions}</p>

          <dl className="kv">
            <dt>URL</dt>
            <dd className="copyable">
              <code>{result.url}</code>
              <button onClick={() => copy('url', result.url)}>
                {copied === 'url' ? 'copied' : 'copy'}
              </button>
            </dd>
            <dt>Secret</dt>
            <dd className="copyable">
              <code>{result.secret}</code>
              <button onClick={() => copy('secret', result.secret)}>
                {copied === 'secret' ? 'copied' : 'copy'}
              </button>
            </dd>
            <dt>Events</dt>
            <dd className="muted">{result.events.join(', ')}</dd>
            <dt>JQL filter</dt>
            <dd className="copyable">
              <code>{result.jql}</code>
              <button onClick={() => copy('jql', result.jql)}>
                {copied === 'jql' ? 'copied' : 'copy'}
              </button>
            </dd>
          </dl>

          <div className="banner soft">
            <b>The secret is shown once.</b> It is stored encrypted and cannot
            be read back — the same contract as an Atlassian API token. If this
            page is closed before it is pasted into Jira, rotate and start
            again.
          </div>
        </section>
      )}
    </div>
  );
}
