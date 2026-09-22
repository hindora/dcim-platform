import { Fragment, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  ApiError, api,
  type Integration, type IntegrationConfig, type IntegrationPolicy,
  type IntegrationTest,
} from '../../../api/client';
import { Tip } from '../../../components/HoverTip';
import { oneLine, relativeTime, untilTime } from '../../../lib/format';
import { PolicyEditor } from './PolicyEditor';
import { InboxTable, LinksTable, OutboxTable } from './DeliveryTables';
import { WebhookPanel } from './WebhookPanel';
import { AssetsPanel } from './AssetsPanel';

const TICKET_TABS = ['Connection', 'Policy', 'Mapping', 'Webhook', 'Assets',
  'Delivery'] as const;
/** A paging integration has no project, no request type, no webhook of this
 *  kind and no CMDB - an alert is not an issue. Showing those tabs greyed out
 *  would be four places to explain that; not showing them is one. */
const OPS_TABS = ['Connection', 'Policy', 'Delivery'] as const;
type Tab = typeof TICKET_TABS[number];

/** Everything about one integration, behind a button.
 *
 *  Tabbed rather than one long form because the five questions have different
 *  audiences and different lifetimes: the connection is set once, the policy is
 *  argued over, and Delivery is the only one anybody opens at 03:00.
 */
export function IntegrationSheet({ row, onClose }: {
  row: Integration;
  defaults: IntegrationConfig;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const ops = row.kind === 'jsm_ops';
  const tabs: readonly Tab[] = ops ? OPS_TABS : TICKET_TABS;
  const [tab, setTab] = useState<Tab>('Connection');
  /** Only what MOVED. Sending the resolved document would freeze every
   *  default at today's value for this install, for ever. */
  const [edits, setEdits] = useState<Record<string, unknown>>({});
  const [policyEdits, setPolicyEdits] = useState<Partial<IntegrationPolicy>>({});

  const save = useMutation({
    mutationFn: () => {
      const config: Record<string, unknown> = { ...row.config, ...edits };
      if (Object.keys(policyEdits).length) {
        config.policy = { ...(row.config.policy ?? {}), ...policyEdits };
      }
      return api.patchIntegration(row.id, { config });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['integrations'] });
      onClose();
    },
  });

  const value = <K extends keyof IntegrationConfig>(key: K): IntegrationConfig[K] =>
    (key in edits ? edits[key] : row.effective[key]) as IntegrationConfig[K];

  const set = (key: string, v: unknown) => setEdits((e) => ({ ...e, [key]: v }));
  const dirty = Object.keys(edits).length > 0
    || Object.keys(policyEdits).length > 0;

  return (
    <div className="sheet-scrim" role="dialog" aria-modal="true"
         aria-label={`Configure ${row.name}`}
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="sheet">
        <header className="sheet-head">
          <div>
            <h2>{row.name}</h2>
            <p className="mono">{row.base_url}</p>
          </div>
          <button className="close" onClick={onClose} aria-label="Close">✕</button>
        </header>

        <nav className="sheet-tabs" aria-label="Sections">
          {tabs.map((t) => (
            <button key={t} type="button"
                    className={t === tab ? 'active' : ''}
                    aria-pressed={t === tab}
                    onClick={() => setTab(t)}>{t}</button>
          ))}
        </nav>

        <div className="sheet-body">
          {save.error && (
            <div className="banner">
              {save.error instanceof ApiError && save.error.status === 403
                ? 'Changing a ticketing integration needs an admin account.'
                : String((save.error as Error).message)}
            </div>
          )}

          {tab === 'Connection' && (
            ops ? <OpsConnection row={row} value={value} set={set} />
                : <Connection row={row} value={value} set={set} />
          )}

          {tab === 'Policy' && (
            <PolicyEditor integrationId={row.id} effective={row.effective}
                          onChange={setPolicyEdits} />
          )}

          {tab === 'Mapping' && (
            <Mapping row={row} value={value} set={set} />
          )}

          {tab === 'Webhook' && <WebhookPanel row={row} />}

          {tab === 'Assets' && <AssetsPanel row={row} />}

          {tab === 'Delivery' && (
            <div className="stack">
              <h3>Outbound</h3>
              <OutboxTable integrationId={row.id} />
              <h3>Inbound</h3>
              <InboxTable integrationId={row.id} />
              <h3>Tickets</h3>
              <LinksTable integrationId={row.id} />
            </div>
          )}
        </div>

        {tab !== 'Delivery' && tab !== 'Webhook' && tab !== 'Assets' && (
          <footer className="sheet-foot">
            <span className="muted">
              {dirty ? 'Unsaved changes' : 'Nothing changed'}
            </span>
            <div className="toolbar">
              <button onClick={onClose}>Cancel</button>
              <button className="primary" disabled={!dirty || save.isPending}
                      onClick={() => save.mutate()}>
                {save.isPending ? 'Saving…' : 'Save'}
              </button>
            </div>
          </footer>
        )}
      </section>
    </div>
  );
}

/* --------------------------------------------------------------- tabs */

function Connection({ row, value, set }: {
  row: Integration;
  value: <K extends keyof IntegrationConfig>(k: K) => IntegrationConfig[K];
  set: (k: string, v: unknown) => void;
}) {
  const [test, setTest] = useState<IntegrationTest | null>(null);
  const run = useMutation<IntegrationTest>({
    mutationFn: () => api.testIntegration(row.id),
    onSuccess: setTest,
  });

  const expires = row.secret_expires_at
    ? (new Date(row.secret_expires_at).getTime() - Date.now()) / 86_400_000
    : null;

  return (
    <div className="stack">
      <dl className="kv">
        <dt>Credential</dt>
        <dd>
          <span className="mono">{row.secret_hint ?? '—'}</span>
          <div className="detail">
            {expires === null ? (
              <Tip tip={oneLine(`Atlassian does not expose a token's expiry
                      over the API, so this is a date somebody records here.
                      Without it, nothing warns before it lapses.`)}>
                no expiry recorded
              </Tip>
            ) : expires < 0 ? (
              <span className="critical">
                expired {relativeTime(row.secret_expires_at!)}
              </span>
            ) : (
              <span className={expires <= 30 ? 'warn' : 'muted'}>
                expires {untilTime(row.secret_expires_at!)}
              </span>
            )}
          </div>
        </dd>
        <dt>Deployment</dt>
        <dd className="muted">{row.kind.replace('_', ' ')}</dd>
      </dl>

      <div className="form-grid">
      <label>
        <span>Project key</span>
        <input value={String(value('project_key'))}
               onChange={(e) => set('project_key', e.target.value.toUpperCase())}
               placeholder="DCOPS" />
        <small className="hint">
          The key, not the name — <code>DCOPS</code>, not "DC Operations".
        </small>
      </label>

      <label>
          <span>Issue type</span>
          <input value={String(value('issue_type'))}
                 onChange={(e) => set('issue_type', e.target.value)}
                 list="issue-types" />
          <datalist id="issue-types">
            {(test?.discovered.issue_types ?? []).map((t) => (
              <option key={t} value={t} />
            ))}
          </datalist>
      </label>

      <fieldset className="proto">
        <legend>Service desk</legend>
        <small className="hint">
          Leave both empty for a Jira Software project. On a service project
          both are required: an issue created without a request type lands in
          the project but is malformed on the customer portal.
        </small>
        <div className="form-grid">
          <label>
            <span>Service desk id</span>
            <input value={String(value('service_desk_id'))}
                   onChange={(e) => set('service_desk_id', e.target.value)} />
          </label>
          <label>
            <span>Request type id</span>
            <input value={String(value('request_type_id'))}
                   onChange={(e) => set('request_type_id', e.target.value)}
                   list="request-types" />
            <datalist id="request-types">
              {(test?.discovered.request_types ?? []).map((t) => (
                <option key={t.id} value={t.id}>{t.name}</option>
              ))}
            </datalist>
          </label>
        </div>
      </fieldset>
      </div>

      <div className="toolbar">
        <button onClick={() => run.mutate()} disabled={run.isPending}>
          {run.isPending ? 'Asking Jira…' : 'Test the connection'}
        </button>
        <span className="muted">
          read-only — it creates nothing
        </span>
      </div>

      {run.error && (
        <div className="banner">{String((run.error as Error).message)}</div>
      )}

      {test && (
        <ul className="checks">
          {test.checks.map((c) => (
            <li key={c.name} className={c.ok ? 'ok' : 'bad'}>
              <span className="name">{c.name}</span>
              {c.detail && <span className="detail">{c.detail}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** An Operations integration: a cloud id, and how loudly to page.
 *
 *  Nothing else. There is no project, no issue type and no request type,
 *  because an alert is not an issue - and the alert API is addressed by the
 *  site's cloud id rather than by its URL, which is the one configuration
 *  fact that differs from every other target here.
 */
function OpsConnection({ row, value, set }: {
  row: Integration;
  value: <K extends keyof IntegrationConfig>(k: K) => IntegrationConfig[K];
  set: (k: string, v: unknown) => void;
}) {
  const priorities = value('ops_priority_map');
  const responders = value('ops_responders');

  return (
    <div className="stack">
      <dl className="kv">
        <dt>Credential</dt>
        <dd className="mono">{row.secret_hint ?? '—'}</dd>
        <dt>Cloud id</dt>
        <dd className="mono">
          {row.cloud_id ?? (
            <span className="critical">
              not set — this integration cannot be enabled
            </span>
          )}
        </dd>
      </dl>

      <h3>Urgency</h3>
      <p className="muted">
        <Tip tip={oneLine(`The Operations API does not REJECT an unrecognised
                priority - it silently substitutes P3. A map containing
                "Highest" would page the estate's worst faults at the same
                urgency as its mildest and nothing anywhere would say so.`)}>
          P1 to P5, and nothing else. This is the one setting here that
          prevents a silent failure rather than a loud one.
        </Tip>
      </p>
      <dl className="kv">
        {Object.entries(priorities).map(([severity, p]) => (
          <Fragment key={severity}>
            <dt>{severity}</dt>
            <dd>
              <select value={p}
                      onChange={(e) => set('ops_priority_map',
                        { ...priorities, [severity]: e.target.value })}>
                {['P1', 'P2', 'P3', 'P4', 'P5'].map((o) => (
                  <option key={o} value={o}>{o}</option>
                ))}
              </select>
            </dd>
          </Fragment>
        ))}
      </dl>

      <div className="form-grid">
        <label>
          <span>Responder teams</span>
          <input value={responders.join(', ')}
                 onChange={(e) => set('ops_responders',
                   e.target.value.split(',').map((t) => t.trim()).filter(Boolean))}
                 placeholder="Facilities, Network" />
          <small className="hint">
            Comma separated. Empty leaves routing to the team's own rules in
            JSM, which is usually what an on-call setup already encodes.
          </small>
        </label>
      </div>

      <div className="banner soft">
        <b>Alerts are outbound only.</b> Acknowledging or closing an alert
        inside JSM does not reach this platform — the alarm stays on the
        console until the poll clears it. Use the alarm's own Acknowledge, or
        the DCIM, to stop a page from this side.
      </div>
    </div>
  );
}

function Mapping({ row, value, set }: {
  row: Integration;
  value: <K extends keyof IntegrationConfig>(k: K) => IntegrationConfig[K];
  set: (k: string, v: unknown) => void;
}) {
  const priority = value('priority_map');
  const labels = value('labels');

  return (
    <div className="stack">
      <h3>Severity</h3>
      <p className="muted">
        Priority <b>names</b>, not ids — ids differ per site and a customer can
        rename these. A severity mapped to a name this site does not have is
        dropped from the create, so the ticket still lands, at the project
        default.
      </p>
      <dl className="kv">
        {Object.entries(priority).map(([severity, name]) => (
          <Fragment key={severity}>
            <dt>{severity}</dt>
            <dd>
              <input value={name}
                     onChange={(e) => set('priority_map',
                       { ...priority, [severity]: e.target.value })} />
            </dd>
          </Fragment>
        ))}
      </dl>

      <h3>Labels</h3>
      <p className="muted">
        The taxonomy rides as labels rather than custom fields: they need no
        Jira administrator, cost nothing to create and match in JQL at index
        speed. The fingerprint label is always added.
      </p>
      <div className="form-grid">
      <label>
        <span>Prefix</span>
        <input value={labels.prefix}
               onChange={(e) => set('labels',
                 { ...labels, prefix: e.target.value.replace(/\s+/g, '-') })} />
      </label>
      {(['site', 'room', 'category'] as const).map((k) => (
        <label className="switch" key={k}>
          <input type="checkbox" checked={labels[k]}
                 onChange={(e) => set('labels', { ...labels, [k]: e.target.checked })} />
          <span>Label the {k}</span>
        </label>
      ))}
      </div>

      <h3>Custom fields</h3>
      <p className="muted">
        Optional, and empty means “do not send it”. A create naming a field
        that is not on the project's screen fails <em>entirely</em>, so an
        unmapped field is left out rather than sent empty.
      </p>
      <dl className="kv">
        {Object.entries(value('fields')).map(([key, id]) => (
          <Fragment key={key}>
            <dt>{key.replace('_', ' ')}</dt>
            <dd>
              <input value={id} placeholder="customfield_10101"
                     onChange={(e) => set('fields',
                       { ...value('fields'), [key]: e.target.value })} />
            </dd>
          </Fragment>
        ))}
      </dl>

      <h3>When a fault clears</h3>
      <div className="form-grid">
      <label>
        <span>On clear</span>
        <select value={String(value('close_on_clear'))}
                onChange={(e) => set('close_on_clear', e.target.value)}>
          <option value="comment">Comment on the ticket</option>
          <option value="transition">Comment and transition it</option>
          <option value="none">Do nothing</option>
        </select>
        <small className="hint">
          Commenting is the default because a fault clearing is not the same as
          the work being finished — somebody still has to replace the fan the
          unit is now running without.
        </small>
      </label>

      <label className="switch">
        <input type="checkbox" checked={Boolean(value('allow_clear_on_transition'))}
               onChange={(e) => set('allow_clear_on_transition', e.target.checked)} />
        <span className="warn">Let Jira clear alarms</span>
        <small className="hint">
            Off, and it should stay off. Closing a ticket normally
            <b> acknowledges</b> its alarm; only the poll clears one. Turning
            this on means an engineer who fixed the symptom, or who closed the
            ticket because the part arrives Thursday, silences a live fault
            from outside this platform. Defensible only for conditions nothing
            polls.
        </small>
      </label>
      </div>

      {row.effective.allow_clear_on_transition && (
        <div className="banner">
          This integration currently trusts Jira over the plane.
        </div>
      )}
    </div>
  );
}
