import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../../../api/client';
import { Tip } from '../../../components/HoverTip';
import { oneLine } from '../../../lib/format';

const KINDS = [
  { key: 'jira_cloud', label: 'Jira Cloud — tickets',
    hint: 'An API token on a dedicated Atlassian account, sent as Basic auth.' },
  { key: 'jira_dc', label: 'Jira Data Center — tickets',
    hint: 'A personal access token, sent as a Bearer. Data Center has no API tokens.' },
  { key: 'jsm_ops', label: 'JSM Operations — paging',
    hint: 'Wakes somebody up rather than opening a ticket. Needs the cloud '
        + 'id: the alert API is addressed by it rather than by the site URL.' },
] as const;

/** Adding one. Deliberately short: a project key and a credential.
 *
 *  Everything else has a working default and is edited afterwards, against
 *  the connection test and the policy rehearsal — neither of which can run
 *  until the integration exists.
 */
export function NewIntegration({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [kind, setKind] = useState<'jira_cloud' | 'jira_dc' | 'jsm_ops'>(
    'jira_cloud');
  const [cloudId, setCloudId] = useState('');
  const [name, setName] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [project, setProject] = useState('');
  const [username, setUsername] = useState('');
  const [token, setToken] = useState('');
  const [expires, setExpires] = useState('');

  const create = useMutation({
    mutationFn: () => api.createIntegration({
      kind, name, base_url: baseUrl,
      cloud_id: cloudId || null,
      config: project && kind !== 'jsm_ops' ? { project_key: project } : {},
      secret: {
        username: kind === 'jira_cloud' ? username : undefined,
        token,
        expires_at: expires || null,
      },
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['integrations'] });
      onClose();
    },
  });

  const paging = kind === 'jsm_ops';
  const ready = name && baseUrl && token
    && (kind === 'jira_dc' || username.includes('@'))
    && (!paging || cloudId.trim().length > 0);

  return (
    <div className="sheet-scrim" role="dialog" aria-modal="true"
         aria-label="Add an integration"
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="sheet narrow">
        <header className="sheet-head">
          <div><h2>Add a ticketing integration</h2></div>
          <button className="close" onClick={onClose} aria-label="Close">✕</button>
        </header>

        <div className="sheet-body">
          <div className="form-grid">
          {create.error && (
            <div className="banner">{String((create.error as Error).message)}</div>
          )}

          <label>
            <span>Deployment</span>
            <select value={kind}
                    onChange={(e) => setKind(e.target.value as typeof kind)}>
              {KINDS.map((k) => (
                <option key={k.key} value={k.key}>{k.label}</option>
              ))}
            </select>
            <small className="hint">
              {KINDS.find((k) => k.key === kind)!.hint}
            </small>
          </label>

          <label>
            <span>Name</span>
            <input value={name} onChange={(e) => setName(e.target.value)}
                   placeholder="Facilities service desk" />
            <small className="hint">
              Shown on tickets and in the audit log. Name it after the desk it
              writes to, not after Jira.
            </small>
          </label>

          <label>
            <span>Base URL</span>
            <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
                   placeholder="https://acme.atlassian.net" />
            <small className="hint">
              https only — Basic auth over plain HTTP puts the credential on
              the wire in base64, which is not encryption.
            </small>
          </label>

          {paging ? (
            <label>
              <span>Cloud id</span>
              <input value={cloudId} onChange={(e) => setCloudId(e.target.value)}
                     placeholder="11111111-2222-3333-4444-555555555555" />
              <small className="hint">
                <Tip tip={oneLine(`Visit
                        https://<your-site>.atlassian.net/_edge/tenant_info -
                        the alert API is addressed by this rather than by the
                        site URL, which is the one configuration fact that
                        differs from a ticketing integration.`)}>
                  Required. Not the site URL.
                </Tip>
              </small>
            </label>
          ) : (
            <label>
              <span>Project key</span>
              <input value={project}
                     onChange={(e) => setProject(e.target.value.toUpperCase())}
                     placeholder="DCOPS" />
            </label>
          )}

          {kind !== 'jira_dc' && (
            <label>
              <span>Account email</span>
              <input value={username} onChange={(e) => setUsername(e.target.value)}
                     placeholder="dcim-bot@acme.com" type="email" />
              <small className="hint">
                <Tip tip={oneLine(`A dedicated service account rather than a
                        person: every ticket this platform opens is attributed
                        to it, and a colleague leaving should not stop the
                        integration.`)}>
                  The Atlassian account the API token belongs to.
                </Tip>
              </small>
            </label>
          )}

          <label>
            <span>{kind === 'jira_dc' ? 'Personal access token' : 'API token'}</span>
            <input value={token} onChange={(e) => setToken(e.target.value)}
                   type="password" autoComplete="off" />
            <small className="hint">
              Stored encrypted and never readable again — only a hint that says
              what kind of secret exists and how long it is.
            </small>
          </label>

          <label>
            <span>Expires on</span>
            <input type="date" value={expires}
                   onChange={(e) => setExpires(e.target.value)} />
            <small className="hint">
              <Tip tip={oneLine(`Atlassian does not expose a token's expiry
                      over its API, so this is a note to ourselves - and it is
                      what raises an alarm 30 days out. Cloud tokens created
                      after December 2024 last at most a year.`)}>
                Optional, and worth filling in: without it, nothing warns you
                before ticketing stops.
              </Tip>
            </small>
          </label>
          </div>
        </div>

        <footer className="sheet-foot">
          <span className="muted">
            Created disabled — test the connection first.
          </span>
          <div className="toolbar">
            <button onClick={onClose}>Cancel</button>
            <button className="primary" disabled={!ready || create.isPending}
                    onClick={() => create.mutate()}>
              {create.isPending ? 'Adding…' : 'Add'}
            </button>
          </div>
        </footer>
      </section>
    </div>
  );
}
