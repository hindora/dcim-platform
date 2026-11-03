import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ApiError, api, atLeast, type IntegrationPolicy, type NotificationDelivery,
  type NotificationRule, type NotificationRuleInput, type SmtpSettings, type SmtpUpdate,
} from '../../api/client';
import { Seg } from '../../components/estate';
import { Notice } from '../../components/Notice';
import { NumberInput } from '../../components/NumberInput';
import { Pagination } from '../../components/Pagination';
import { relativeTime } from '../../lib/format';
import { Facets } from './components/PolicyEditor';

const SEVERITIES = ['CRITICAL', 'MAJOR', 'MINOR', 'WARNING', 'INFO'] as const;
const CATEGORIES = ['power', 'cooling', 'environmental', 'it_equipment',
  'network', 'capacity', 'visibility', 'security', 'fire'] as const;
const CLASSES = ['alarm', 'alert'] as const;

const DEFAULT_POLICY: IntegrationPolicy = {
  response_classes: ['alarm'], min_severity: 'MAJOR', categories: [...CATEGORIES],
  exclude_symptoms: true, exclude_shelved: true, dwell_s: 0, alarm_types: [],
};

function errText(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 403) return 'Only an admin can change notification settings.';
    return e.message;
  }
  return String(e);
}

/** Who is emailed about which alarms, and through which relay.
 *
 *  Separate from Ticketing on purpose: a ticket queues work for later, an email
 *  tells the person on call now. Three parts, top to bottom in the order they
 *  are set up: the relay, the rules, and what was actually sent. */
export function Notifications() {
  const isAdmin = atLeast('admin');
  return (
    <div className="stack">
      <div>
        <h2>Notifications</h2>
        <p className="subtitle">
          Email the right people when an alarm starts, escalates or clears.
        </p>
      </div>
      {!isAdmin && (
        <Notice tone="info">You can see these settings. Only an admin can change them.</Notice>
      )}
      <MailServer isAdmin={isAdmin} />
      <Rules isAdmin={isAdmin} />
      <Deliveries />
    </div>
  );
}

// ------------------------------------------------------------------ relay

function MailServer({ isAdmin }: { isAdmin: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ['smtp'], queryFn: api.smtp });
  const [form, setForm] = useState<SmtpSettings | null>(null);
  const [password, setPassword] = useState<string | undefined>(undefined);
  const [testTo, setTestTo] = useState('');
  const [saved, setSaved] = useState(false);

  useEffect(() => { if (q.data) setForm(q.data); }, [q.data]);

  const save = useMutation({
    mutationFn: () => {
      const f = form as SmtpSettings;
      const body: SmtpUpdate = {
        enabled: f.enabled, host: f.host, port: f.port, security: f.security,
        username: f.username, from_address: f.from_address, from_name: f.from_name,
        reply_to: f.reply_to, link_base_url: f.link_base_url,
      };
      if (password !== undefined) body.password = password;
      return api.saveSmtp(body);
    },
    onSuccess: (next) => {
      qc.setQueryData(['smtp'], next);
      setPassword(undefined);
      setSaved(true);
    },
  });
  const test = useMutation({ mutationFn: () => api.testSmtp(testTo.trim()) });

  if (q.error) return <Notice tone="error">Could not load the mail server: {errText(q.error)}</Notice>;
  if (!form) return null;

  const set = <K extends keyof SmtpSettings>(k: K, v: SmtpSettings[K]) => {
    setForm({ ...form, [k]: v });
    setSaved(false);
  };
  const plain = form.security === 'none';
  const dirty = password !== undefined || JSON.stringify(form) !== JSON.stringify(q.data);

  return (
    <fieldset className="proto">
      <legend>Mail server</legend>
      <p className="muted">
        The site's SMTP relay (Exchange, Microsoft 365, Postfix). Alarms are handed to it;
        it delivers them.
      </p>
      {save.error && <Notice tone="error">{errText(save.error)}</Notice>}
      {saved && !dirty && (
        <Notice tone="ok">
          Saved. Email is {form.enabled ? 'on' : 'off'}.
        </Notice>
      )}
      <fieldset disabled={!isAdmin} style={{ border: 0, padding: 0, margin: 0 }}>
        <label className="switch">
          <input type="checkbox" checked={form.enabled}
                 onChange={(e) => set('enabled', e.target.checked)} />
          <span>Send alarm emails</span>
          <small className="hint">
            Off stops every rule at once. Emails already queued are dropped, not held.
          </small>
        </label>
        <div className="form-grid">
          <label><span>Relay host</span>
            <input value={form.host} placeholder="smtp.office365.com"
                   onChange={(e) => set('host', e.target.value)} />
          </label>
          <label><span>Port</span>
            <NumberInput int value={form.port} min={1} max={65535} fallback={587}
                         onChange={(n) => set('port', n)} />
          </label>
          <label><span>Security</span>
            <select value={form.security}
                    onChange={(e) => set('security', e.target.value as SmtpSettings['security'])}>
              <option value="starttls">STARTTLS (usually port 587)</option>
              <option value="tls">TLS from the start (usually port 465)</option>
              <option value="none">None (internal relay, usually port 25)</option>
            </select>
            {plain && (
              <em className="hint">
                Mail crosses the network unencrypted. Only for a relay on a trusted network,
                and without a login.
              </em>
            )}
          </label>
          <label><span>Username</span>
            <input value={form.username} autoComplete="off" placeholder="Leave empty if the relay allows it"
                   onChange={(e) => set('username', e.target.value)} />
          </label>
          <label><span>Password</span>
            <input type="password" autoComplete="new-password"
                   value={password ?? ''}
                   placeholder={form.password_set ? 'Saved. Type to replace it' : 'Not set'}
                   onChange={(e) => { setPassword(e.target.value); setSaved(false); }} />
            {form.password_set && isAdmin && (
              <em className="hint">
                <button type="button" className="link"
                        onClick={() => { setPassword(''); setSaved(false); }}>
                  Remove the saved password
                </button>
                {password === '' && ' (removed when you save)'}
              </em>
            )}
          </label>
          <label><span>From address</span>
            <input type="email" value={form.from_address} placeholder="dcim-alarms@example.com"
                   onChange={(e) => set('from_address', e.target.value)} />
          </label>
          <label><span>From name</span>
            <input value={form.from_name} placeholder="Defaults to your organisation name + DCIM"
                   onChange={(e) => set('from_name', e.target.value)} />
          </label>
          <label><span>Reply-to</span>
            <input type="email" value={form.reply_to} placeholder="Optional, such as the NOC mailbox"
                   onChange={(e) => set('reply_to', e.target.value)} />
          </label>
          <label><span>Link base URL</span>
            <input value={form.link_base_url} placeholder="https://dcim.example.com"
                   onChange={(e) => set('link_base_url', e.target.value)} />
            <em className="hint">Each email links to its alarm at this address.</em>
          </label>
        </div>
      </fieldset>
      {isAdmin && (
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8, flexWrap: 'wrap' }}>
          <button className="primary" disabled={!dirty || save.isPending}
                  onClick={() => save.mutate()}>
            {save.isPending ? 'Saving…' : 'Save mail server'}
          </button>
          <span className="muted" style={{ marginLeft: 16 }}>Send a test to</span>
          <input type="email" value={testTo} placeholder="you@example.com"
                 aria-label="Test email recipient" style={{ width: 220 }}
                 onChange={(e) => { setTestTo(e.target.value); test.reset(); }} />
          <button disabled={!testTo.trim() || dirty || test.isPending}
                  title={dirty ? 'Save first: the test uses the saved settings' : undefined}
                  onClick={() => test.mutate()}>
            {test.isPending ? 'Sending…' : 'Send test email'}
          </button>
        </div>
      )}
      {test.error && <Notice tone="error">Test failed: {errText(test.error)}</Notice>}
      {test.data && (
        <Notice tone="ok">
          The relay accepted the test for {test.data.to}. If it does not arrive, check spam.
        </Notice>
      )}
      {q.data?.updated_by && (
        <p className="muted">Last changed by {q.data.updated_by}, {relativeTime(q.data.updated_at)}.</p>
      )}
    </fieldset>
  );
}

// ------------------------------------------------------------------ rules

function Rules({ isAdmin }: { isAdmin: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ['notification-rules'], queryFn: api.notificationRules });
  const [editing, setEditing] = useState<NotificationRule | 'new' | null>(null);
  const toggle = useMutation({
    mutationFn: (r: NotificationRule) => api.updateNotificationRule(r.id, { enabled: !r.enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['notification-rules'] }),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteNotificationRule(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['notification-rules'] }),
  });
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const items = q.data?.items ?? [];

  return (
    <section className="stack">
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
        <h3 style={{ margin: 0 }}>Rules</h3>
        <span className="muted">Each rule sends to its own people, for its own alarms.</span>
        {isAdmin && editing === null && (
          <button className="primary" style={{ marginLeft: 'auto' }}
                  onClick={() => setEditing('new')}>Add rule</button>
        )}
      </div>
      {q.error && <Notice tone="error">Could not load the rules: {errText(q.error)}</Notice>}
      {(toggle.error || remove.error) && (
        <Notice tone="error">{errText(toggle.error ?? remove.error)}</Notice>
      )}

      {editing !== null && (
        <RuleForm rule={editing === 'new' ? null : editing} onDone={() => setEditing(null)} />
      )}

      {q.data && items.length === 0 && editing === null && (
        <p className="muted">
          No rules yet, so no alarm emails go out. A common start: one rule for the on-call
          NOC with CRITICAL and MAJOR alarms, and one for facilities with power and cooling.
        </p>
      )}
      {items.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Rule</th><th>Sends</th><th>To</th><th>Last 24 h</th><th>State</th>
              {isAdmin && <th aria-label="Actions" />}
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.id}>
                <td>{r.name}</td>
                <td className="detail">{r.summary}</td>
                <td className="muted" title={r.recipients.join(', ')}>
                  {r.recipients.length === 1 ? r.recipients[0] : `${r.recipients.length} addresses`}
                </td>
                <td className="muted">
                  {r.sent_24h} {r.sent_24h === 1 ? 'email' : 'emails'}
                  {r.last_sent_at && <>, last {relativeTime(r.last_sent_at)}</>}
                </td>
                <td className={r.enabled ? '' : 'muted'}>{r.enabled ? 'On' : 'Off'}</td>
                {isAdmin && (
                  <td style={{ whiteSpace: 'nowrap', textAlign: 'right' }}>
                    <button onClick={() => setEditing(r)}>Edit</button>{' '}
                    <button onClick={() => toggle.mutate(r)} disabled={toggle.isPending}>
                      {r.enabled ? 'Turn off' : 'Turn on'}
                    </button>{' '}
                    {confirmDelete === r.id ? (
                      <>
                        <button className="danger" onClick={() => remove.mutate(r.id)}>
                          Delete {r.name}
                        </button>{' '}
                        <button onClick={() => setConfirmDelete(null)}>Keep</button>
                      </>
                    ) : (
                      <button onClick={() => setConfirmDelete(r.id)}>Delete</button>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function RuleForm({ rule, onDone }: { rule: NotificationRule | null; onDone: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState(rule?.name ?? '');
  const [recipients, setRecipients] = useState((rule?.recipients ?? []).join(', '));
  const [policy, setPolicy] = useState<IntegrationPolicy>(rule?.policy ?? DEFAULT_POLICY);
  const [onClear, setOnClear] = useState(rule?.notify_on_clear ?? true);
  const [onEsc, setOnEsc] = useState(rule?.notify_on_escalation ?? true);
  const [maxPerHour, setMaxPerHour] = useState(rule?.max_per_hour ?? 30);

  const preview = useMutation({ mutationFn: () => api.previewNotificationRule(policy, 7) });
  const save = useMutation({
    mutationFn: () => {
      const body: NotificationRuleInput = {
        name, recipients, policy, notify_on_clear: onClear,
        notify_on_escalation: onEsc, max_per_hour: maxPerHour,
      };
      return rule ? api.updateNotificationRule(rule.id, body) : api.createNotificationRule(body);
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['notification-rules'] });
      onDone();
    },
  });

  const set = <K extends keyof IntegrationPolicy>(k: K, v: IntegrationPolicy[K]) => {
    setPolicy({ ...policy, [k]: v });
    preview.reset();
  };
  const toggleIn = (key: 'categories' | 'response_classes', value: string,
                    all: readonly string[]) => {
    const cur = policy[key];
    const next = cur.includes(value) ? cur.filter((v) => v !== value) : [...cur, value];
    set(key, next.length ? all.filter((a) => next.includes(a)) : [...all]);
  };
  const delayMin = Math.round(policy.dwell_s / 60);

  return (
    <fieldset className="proto">
      <legend>{rule ? `Edit ${rule.name}` : 'New rule'}</legend>
      {save.error && <Notice tone="error">{errText(save.error)}</Notice>}
      <div className="form-grid">
        <label><span>Name</span>
          <input value={name} maxLength={80} placeholder="NOC on-call"
                 onChange={(e) => setName(e.target.value)} />
        </label>
        <label><span>Send to</span>
          <textarea rows={2} value={recipients}
                    placeholder="noc@example.com, facilities@example.com"
                    onChange={(e) => setRecipients(e.target.value)} />
          <em className="hint">
            Up to 20 addresses, separated by commas. For more, use a distribution list. An
            email-to-SMS gateway address works here too.
          </em>
        </label>

        <div className="group" role="group" aria-labelledby="nr-sev">
          <span id="nr-sev">Severity</span>
          <Seg value={policy.min_severity} label="Minimum severity"
               onChange={(v) => set('min_severity', v)}
               options={SEVERITIES.map((s) => ({ key: s, label: s }))} />
          <small className="hint">This severity and anything worse.</small>
        </div>
        <div className="group" role="group" aria-labelledby="nr-class">
          <span id="nr-class">Kind</span>
          <Facets values={CLASSES} on={policy.response_classes} label="Kind"
                  onToggle={(v) => toggleIn('response_classes', v, CLASSES)}
                  onAll={() => set('response_classes', [...CLASSES])} />
          <small className="hint">
            <b>alarm</b> needs someone to act now. <b>alert</b> is for whoever plans the work;
            emailing alerts to on-call is how mailboxes get ignored.
          </small>
        </div>
        <div className="group" role="group" aria-labelledby="nr-dom">
          <span id="nr-dom">Domains</span>
          <Facets values={CATEGORIES} on={policy.categories} label="Domains"
                  onToggle={(v) => toggleIn('categories', v, CATEGORIES)}
                  onAll={() => set('categories', [...CATEGORIES])} />
        </div>
        <label><span>Wait before the first email (minutes)</span>
          <NumberInput int value={delayMin} min={0} max={60} fallback={0}
                       onChange={(n) => set('dwell_s', Math.max(0, Math.min(60, n)) * 60)} />
          <em className="hint">
            0 sends at once. A short wait skips conditions that clear by themselves, at the
            cost of a later page for the ones that do not.
          </em>
        </label>
        <label><span>At most this many emails an hour</span>
          <NumberInput int value={maxPerHour} min={1} max={500} fallback={30}
                       onChange={setMaxPerHour} />
          <em className="hint">
            Past this, new alarms are logged as suppressed instead of sent. Clears still go out.
          </em>
        </label>
        <label className="switch">
          <input type="checkbox" checked={onClear} onChange={(e) => setOnClear(e.target.checked)} />
          <span>Email when the alarm clears</span>
        </label>
        <label className="switch">
          <input type="checkbox" checked={onEsc} onChange={(e) => setOnEsc(e.target.checked)} />
          <span>Email when the alarm escalates</span>
        </label>
        <label className="switch">
          <input type="checkbox" checked={policy.exclude_symptoms}
                 onChange={(e) => set('exclude_symptoms', e.target.checked)} />
          <span>Root causes only</span>
          <small className="hint">
            One switch failure is one email, not one for each device behind it.
          </small>
        </label>
        <label className="switch">
          <input type="checkbox" checked={policy.exclude_shelved}
                 onChange={(e) => set('exclude_shelved', e.target.checked)} />
          <span>Skip planned work</span>
          <small className="hint">Nothing for alarms shelved by a maintenance window.</small>
        </label>
      </div>

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8, flexWrap: 'wrap' }}>
        <button className="primary" disabled={!name.trim() || !recipients.trim() || save.isPending}
                onClick={() => save.mutate()}>
          {save.isPending ? 'Saving…' : rule ? 'Save rule' : 'Add rule'}
        </button>
        <button onClick={onDone}>Cancel</button>
        <button style={{ marginLeft: 'auto' }} onClick={() => preview.mutate()}
                disabled={preview.isPending}>
          {preview.isPending ? 'Checking…' : 'How many emails would this send?'}
        </button>
      </div>
      {preview.error && <Notice tone="error">{errText(preview.error)}</Notice>}
      {preview.data && (
        <Notice tone={preview.data.per_day > 20 ? 'warn' : 'info'}>
          Over the last {preview.data.days} days this rule would have emailed about{' '}
          {preview.data.conditions} {preview.data.conditions === 1 ? 'condition' : 'conditions'}
          {' '}(about {preview.data.per_day} a day
          {onClear ? ', plus one email each when they cleared' : ''}).
          {preview.data.per_day > 20 && ' That many emails a day tends to get filtered unread.'}
        </Notice>
      )}
    </fieldset>
  );
}

// ------------------------------------------------------------------ log

const STATUS_WORD: Record<NotificationDelivery['status'], string> = {
  pending: 'Waiting', sending: 'Sending', sent: 'Sent', skipped: 'Not needed',
  suppressed: 'Suppressed', failed: 'Failed',
};

function Deliveries() {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [status, setStatus] = useState<string>('');
  const q = useQuery({
    queryKey: ['notification-deliveries', page, pageSize, status],
    queryFn: () => api.notificationDeliveries({
      limit: pageSize, offset: (page - 1) * pageSize, status: status || undefined,
    }),
    refetchInterval: 15_000,
  });
  const items = q.data?.items ?? [];

  return (
    <section className="stack">
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
        <h3 style={{ margin: 0 }}>Recent emails</h3>
        <span className="muted">Kept for 30 days.</span>
        <select style={{ marginLeft: 'auto' }} value={status} aria-label="Filter by state"
                onChange={(e) => { setStatus(e.target.value); setPage(1); }}>
          <option value="">Every state</option>
          <option value="sent">Sent</option>
          <option value="pending">Waiting</option>
          <option value="failed">Failed</option>
          <option value="suppressed">Suppressed</option>
          <option value="skipped">Not needed</option>
        </select>
      </div>
      {q.error && <Notice tone="error">Could not load the log: {errText(q.error)}</Notice>}
      {q.data && items.length === 0 && (
        <p className="muted">
          {status ? 'Nothing in this state.' : 'No emails yet. They appear here as alarms match a rule.'}
        </p>
      )}
      {items.length > 0 && (
        <>
          <table>
            <thead>
              <tr><th>When</th><th>Rule</th><th>Email</th><th>State</th><th>Detail</th></tr>
            </thead>
            <tbody>
              {items.map((d) => (
                <tr key={d.id}>
                  <td className="muted" style={{ whiteSpace: 'nowrap' }}>
                    {relativeTime(d.sent_at ?? d.created_at)}
                  </td>
                  <td>{d.rule_name}</td>
                  <td>{d.subject ?? `${d.kind.replace('alarm_', '')}: ${d.device_name ?? 'alarm'}`}</td>
                  <td className={d.status === 'failed' ? 'critical' : d.status === 'sent' ? '' : 'muted'}>
                    {STATUS_WORD[d.status]}
                    {d.status === 'pending' && d.attempts > 0 && ` (retry ${d.attempts})`}
                  </td>
                  <td className="detail">{d.note ?? ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination page={page} pageSize={pageSize} shown={items.length}
                      total={q.data?.total} hasNext={page * pageSize < (q.data?.total ?? 0)}
                      noun="emails" onPage={setPage}
                      onSize={(n) => { setPageSize(n); setPage(1); }} />
        </>
      )}
    </section>
  );
}
