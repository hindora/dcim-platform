import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ApiError,
  api,
  type PollProfileBody,
  type PollProfileSummary,
  type PollProfilesPage,
  type ProfileUsage,
} from '../../api/client';
import { oneLine } from '../../lib/format';
import { Tip } from '../../components/HoverTip';

/** Poll profiles: how often each endpoint is asked, and for what.
 *
 *  The endpoint counts are not decoration. A profile is shared - `redfish-60s`
 *  carries 310 endpoints - so every number on this page is multiplied by the
 *  count beside it before it reaches a network, and the collectors pick the
 *  change up within one assignment interval. The count is the whole reason
 *  this screen shows a list before it shows a form. */
export function PollProfiles() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<PollProfileSummary | null>(null);
  const [creating, setCreating] = useState(false);

  const page = useQuery<PollProfilesPage>({
    queryKey: ['poll-profiles'],
    queryFn: () => api.pollProfiles(),
  });

  if (page.isLoading) return <p className="muted">Loading…</p>;
  if (page.error) {
    return <div className="banner">Could not load poll profiles.</div>;
  }
  const profiles = page.data?.profiles ?? [];

  return (
    <div className="stack">
      <div>
        <h2>Poll profiles</h2>
        <p className="subtitle">
          How often an endpoint is asked, how long the collector waits, and
          which metric groups it asks for. A profile is shared by every
          endpoint that points at it — the count in each row is how many
          devices an edit moves.
        </p>
      </div>

      <div className="toolbar">
        <button className="primary" onClick={() => setCreating(true)}>
          New profile
        </button>
      </div>

      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th className="num">Interval</th>
            <th className="num">Timeout</th>
            <th className="num">Retries</th>
            <th>Metric groups</th>
            <th>Protocols</th>
            <th className="num">Endpoints</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {profiles.map((p) => (
            <tr key={p.id}>
              <td className="mono">{p.name}</td>
              <td className="num">
                {p.interval_s === 0
                  ? <Tip tip="the device pushes on its own schedule">
                      pushed
                    </Tip>
                  : `${p.interval_s}s`}
              </td>
              <td className="num muted">{p.timeout_ms} ms</td>
              <td className="num muted">{p.retries}</td>
              <td className="muted">
                {p.metric_groups.length
                  ? p.metric_groups.join(', ')
                  : <GroupsNote protocols={p.protocols ?? []} />}
              </td>
              <td className="muted">{(p.protocols ?? []).join(', ') || '—'}</td>
              <td className="num">
                {p.endpoints.toLocaleString()}
                {p.endpoints_enabled != null
                  && p.endpoints_enabled !== p.endpoints && (
                  <span className="muted">
                    {' · '}{p.endpoints_enabled.toLocaleString()} enabled
                  </span>
                )}
              </td>
              <td><button onClick={() => setEditing(p)}>Edit</button></td>
            </tr>
          ))}
        </tbody>
      </table>

      {(editing || creating) && (
        <ProfileSheet
          profile={editing}
          options={page.data!}
          onClose={() => { setEditing(null); setCreating(false); }}
          onSaved={() => {
            qc.invalidateQueries({ queryKey: ['poll-profiles'] });
            qc.invalidateQueries({ queryKey: ['endpoint-options'] });
            setEditing(null); setCreating(false);
          }}
        />
      )}
    </div>
  );
}

/** Why an empty group list is normal on most protocols.
 *
 *  Metric groups are read by the SNMP adapter and nothing else: gNMI subscribes
 *  from its own mapping file, BACnet reads its object map, Modbus its register
 *  templates. An empty cell on those rows is correct, and saying so here stops
 *  somebody "fixing" it by inventing a group name. */
function GroupsNote({ protocols }: { protocols: string[] }) {
  const snmp = protocols.includes('snmp');
  return (
    <Tip className="muted" tip={snmp
      ? 'An SNMP profile with no groups collects nothing.'
      : 'Only the SNMP adapter reads metric groups; this protocol selects what to read from its own mapping file.'}>
      {snmp ? '— none, collects nothing' : 'n/a'}
    </Tip>
  );
}

function ProfileSheet({
  profile, options, onClose, onSaved,
}: {
  profile: PollProfileSummary | null;
  options: PollProfilesPage;
  onClose: () => void;
  onSaved: () => void;
}) {
  const isNew = profile === null;
  const limits = options.limits;
  const groups = options.metric_groups.snmp ?? [];

  const [form, setForm] = useState(() => ({
    name: profile?.name ?? '',
    interval_s: String(profile?.interval_s ?? 60),
    timeout_ms: String(profile?.timeout_ms ?? 5000),
    retries: String(profile?.retries ?? 1),
    push_enabled: profile?.push_enabled ?? false,
    metric_groups: new Set(profile?.metric_groups ?? []),
  }));

  // What this edit would move. Fetched for an existing profile only: a new one
  // starts with nothing following it.
  const usage = useQuery<ProfileUsage>({
    queryKey: ['poll-profile-usage', profile?.id],
    queryFn: () => api.pollProfileUsage(profile!.id),
    enabled: Boolean(profile),
  });

  const errors = useMemo(() => validate(form, limits), [form, limits]);
  // The two calls return different shapes - a created profile, or a summary of
  // what an edit moved - and the sheet needs neither, only that it worked.
  const save = useMutation<void>({
    mutationFn: async () => {
      if (isNew) await api.createPollProfile(body(form, true));
      else await api.updatePollProfile(profile!.id, body(form, false));
    },
    onSuccess: onSaved,
  });

  const set = (k: keyof typeof form, v: unknown) =>
    setForm((f) => ({ ...f, [k]: v }));
  const toggleGroup = (g: string) => setForm((f) => {
    const next = new Set(f.metric_groups);
    if (next.has(g)) next.delete(g); else next.add(g);
    return { ...f, metric_groups: next };
  });

  const pushed = form.push_enabled && Number(form.interval_s) === 0;
  const moved = usage.data?.endpoints ?? 0;

  return (
    <div className="sheet-scrim" role="dialog" aria-modal="true"
         aria-label={isNew ? 'New poll profile' : `Edit ${profile!.name}`}
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="sheet narrow">
        <header className="sheet-head">
          <div>
            <h2>{isNew ? 'New poll profile' : profile!.name}</h2>
            <p>
              {isNew
                ? 'Nothing follows a new profile until endpoints are moved onto it.'
                : "Every endpoint that follows this profile changes with it."}
            </p>
          </div>
          <button className="close" onClick={onClose} aria-label="Close">✕</button>
        </header>

        <div className="sheet-body">
          {save.error && (
            <div className="banner">
              {save.error instanceof ApiError && save.error.status === 403
                ? 'Creating and editing poll profiles needs an admin account. '
                  + 'Moving one endpoint between existing profiles does not.'
                : String((save.error as Error).message)}
            </div>
          )}

          {!isNew && moved > 0 && (
            <div className="banner soft">
              <strong>{moved.toLocaleString()} endpoints</strong> across{' '}
              {(usage.data?.devices ?? 0).toLocaleString()} devices follow this
              profile and will all change:{' '}
              {(usage.data?.breakdown ?? [])
                .map((b) => `${b.endpoints} ${b.protocol} on ${b.device_type}`)
                .join(', ')}.
            </div>
          )}

          <fieldset className="proto">
            <legend>Identity</legend>
            <div className="form-grid">
              <label>
                <span>Name</span>
                <input value={form.name} disabled={!isNew}
                       onChange={(e) => set('name', e.target.value)}
                       placeholder="snmp-edge-300s" />
                <Hint error={errors.name}
                      help={isNew
                        ? 'Lowercase, digits and hyphens.'
                        : 'A profile cannot be renamed.'}
                      detail={isNew
                        ? 'Convention here is protocol-purpose-interval, so a '
                          + 'reader can tell what a profile is for from the '
                          + 'endpoint list alone.'
                        : 'app/importer/endpoints.py selects profiles by name, '
                          + 'so a rename would send the next import to a '
                          + 'different profile without failing. Create a new '
                          + 'one and move the endpoints.'} />
              </label>
            </div>
          </fieldset>

          <fieldset className="proto">
            <legend>Schedule</legend>
            <div className="form-grid">
              <label className="switch">
                <span>Device pushes on its own schedule</span>
                <input type="checkbox" checked={form.push_enabled}
                       onChange={(e) => {
                         set('push_enabled', e.target.checked);
                         if (e.target.checked) set('interval_s', '0');
                       }} />
                <Hint help="For gNMI subscriptions and Redfish events."
                      detail="A pushed endpoint is never also polled - the
                              duplicate samples would be indistinguishable from
                              real ones." />
              </label>

              <label>
                <span>Interval</span>
                <input value={form.interval_s} inputMode="numeric"
                       disabled={pushed}
                       onChange={(e) => set('interval_s', e.target.value)} />
                <Hint error={errors.interval_s}
                      help={pushed
                        ? 'The device decides when to send.'
                        : `Seconds between polls, ${limits.min_interval_s} at the fastest.`}
                      detail={pushed ? '' : 'Multiplied by every endpoint that '
                        + 'follows this profile, so a number typed here reaches '
                        + 'a network hundreds of times over.'} />
              </label>

              <label>
                <span>Timeout</span>
                <input value={form.timeout_ms} inputMode="numeric"
                       onChange={(e) => set('timeout_ms', e.target.value)} />
                <Hint error={errors.timeout_ms}
                      help="Milliseconds to wait for one answer."
                      detail="Gear behind a serial gateway genuinely needs
                              seconds, not milliseconds: the gateway forwards
                              one transaction at a time." />
              </label>

              <label>
                <span>Retries</span>
                <input value={form.retries} inputMode="numeric"
                       onChange={(e) => set('retries', e.target.value)} />
                <Hint error={errors.retries} help={worstCase(form)}
                      detail="(retries + 1) x timeout is how long one endpoint
                              can hold a worker. Longer than the interval and
                              the next cycle starts before the last gave up." />
              </label>
            </div>
          </fieldset>

          <fieldset className="proto">
            <legend>What it collects</legend>
            <div className="form-grid">
              <label>
                <span>Metric groups</span>
                <div className="checks">
                  {groups.map((g) => (
                    <label key={g} className="check">
                      <input type="checkbox" checked={form.metric_groups.has(g)}
                             onChange={() => toggleGroup(g)} />
                      <span className="mono">{g}</span>
                    </label>
                  ))}
                </div>
                <Hint help="Read by the SNMP adapter only."
                      detail="gNMI, BACnet and Modbus select what to read from
                              their own mapping files, so these do nothing on a
                              profile used by them. An SNMP profile with none
                              selected collects nothing at all." />
              </label>
            </div>
          </fieldset>
        </div>

        <div className="sheet-foot">
          <span className="muted">
            {Object.keys(errors).length > 0
              ? 'Fix the highlighted fields'
              : isNew ? 'Nothing follows it yet'
                : moved > 0 ? `Moves ${moved.toLocaleString()} endpoints`
                  : 'Nothing follows it'}
          </span>
          <span className="spacer" />
          <button onClick={onClose}>Cancel</button>
          <button className="primary"
                  disabled={Object.keys(errors).length > 0 || save.isPending}
                  onClick={() => save.mutate()}>
            {save.isPending ? 'Saving…' : isNew ? 'Create' : 'Save'}
          </button>
        </div>
      </section>
    </div>
  );
}

/** One short line under a field, with the reasoning behind a hover.
 *
 *  An error replaces the line rather than joining it: when something is wrong,
 *  what is wrong is the only thing worth reading. */
function Hint({ help, detail, error }: {
  help: string;
  detail?: string;
  error?: string;
}) {
  if (error) return <em className="hint bad">{error}</em>;
  if (!detail) return <em className="hint">{help}</em>;
  return (
    <em className="hint">
      <Tip tip={oneLine(detail)}>
        {help}<span className="why"> ?</span>
      </Tip>
    </em>
  );
}

type Form = {
  name: string; interval_s: string; timeout_ms: string; retries: string;
  push_enabled: boolean; metric_groups: Set<string>;
};

/** The same checks the server makes, so a number is refused while the operator
 *  is still looking at it. The server remains the boundary. */
function validate(form: Form, limits: PollProfilesPage['limits']) {
  const errors: Record<string, string> = {};
  const interval = Number(form.interval_s);
  const timeout = Number(form.timeout_ms);
  const retries = Number(form.retries);

  if (form.name && !/^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$/.test(form.name))
    errors.name = 'Lowercase letters, digits and hyphens, 3-64 characters.';

  if (!Number.isInteger(interval) || interval < 0)
    errors.interval_s = 'Whole seconds.';
  else if (interval === 0 && !form.push_enabled)
    errors.interval_s = 'Interval 0 only makes sense when the device pushes.';
  else if (interval > 0 && interval < limits.min_interval_s)
    errors.interval_s = `The shortest interval allowed is ${limits.min_interval_s}s.`;

  if (!Number.isInteger(timeout) || timeout < limits.min_timeout_ms
      || timeout > limits.max_timeout_ms)
    errors.timeout_ms = `Between ${limits.min_timeout_ms} and ${limits.max_timeout_ms} ms.`;

  if (!Number.isInteger(retries) || retries < 0 || retries > limits.max_retries)
    errors.retries = `0 to ${limits.max_retries}.`;

  // The arithmetic nobody does by hand: one endpoint can hold a worker for
  // (retries + 1) x timeout, and if that exceeds the interval the next cycle
  // starts before the last gave up.
  if (!errors.interval_s && !errors.timeout_ms && !errors.retries
      && interval > 0 && (retries + 1) * timeout > interval * 1000)
    errors.retries = `${retries} retries at ${timeout} ms is up to `
      + `${((retries + 1) * timeout / 1000).toFixed(0)}s, longer than the `
      + `${interval}s interval.`;

  return errors;
}

function worstCase(form: Form): string {
  const worst = (Number(form.retries) + 1) * Number(form.timeout_ms);
  if (!Number.isFinite(worst)) return 'Attempts after the first.';
  return `Attempts after the first. Worst case ${(worst / 1000).toFixed(1)}s.`;
}

function body(form: Form, creating: boolean): PollProfileBody {
  const out: PollProfileBody = {
    interval_s: Number(form.interval_s),
    timeout_ms: Number(form.timeout_ms),
    retries: Number(form.retries),
    push_enabled: form.push_enabled,
    metric_groups: [...form.metric_groups],
  };
  if (creating) out.name = form.name;
  return out;
}
