import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, api, atLeast, type OrganisationSetting } from '../../api/client';
import { relativeTime } from '../../lib/format';
import { Notice } from '../../components/Notice';

/** The organisation's name: the Home headline, the login card, the tab title.
 *
 *  Instance-wide, so admin-only and audited. An operator with two instances
 *  open tells them apart by this name, which is why it is the estate owner's
 *  and never the product's. Clearing it hands the name back to the server's
 *  DCIM_ORG_NAME, and then to the product name. */
export function Organisation() {
  const qc = useQueryClient();
  const isAdmin = atLeast('admin');
  const q = useQuery({ queryKey: ['organisation'], queryFn: api.organisation });
  const [draft, setDraft] = useState('');
  const [saved, setSaved] = useState<string | null>(null);

  // Seed the field once the current value arrives, and again after a save.
  useEffect(() => {
    if (q.data) setDraft(q.data.saved_name ?? q.data.org_name);
  }, [q.data]);

  const save = useMutation({
    mutationFn: (name: string | null) => api.saveOrganisation(name),
    onSuccess: (next) => {
      qc.setQueryData(['organisation'], next);
      // The shell's headline, login card and tab title all read ['instance'].
      void qc.invalidateQueries({ queryKey: ['instance'] });
      setSaved(next.org_name);
    },
  });

  const data = q.data;
  const max = data?.max_length ?? 80;
  const trimmed = draft.split(/\s+/).filter(Boolean).join(' ');
  const current = data ? (data.saved_name ?? data.org_name) : '';
  const tooLong = trimmed.length > max;
  const unchanged = trimmed === current;
  const problem = !trimmed ? 'Type a name, or use the server default below.'
    : tooLong ? `${trimmed.length} characters; the limit is ${max}.`
    : null;

  return (
    <div className="stack">
      <div>
        <h2>Organisation</h2>
        <p className="subtitle">
          The name on the Home page, the login card and the browser tab.
        </p>
      </div>

      {q.error && <Notice tone="error">Could not load the organisation name: {errText(q.error)}</Notice>}
      {!isAdmin && data && (
        <Notice tone="info">Only an admin can change the organisation name.</Notice>
      )}
      {save.error && <Notice tone="error">{errText(save.error)}</Notice>}
      {saved && !save.isPending && !save.error && (
        <Notice tone="ok">Saved. Pages now show “{saved}”.</Notice>
      )}

      {data && (
        <div>
          <div className="form-grid">
            <label>
              <span>Organisation name</span>
              <input
                value={draft}
                maxLength={max + 20}
                readOnly={!isAdmin}
                aria-invalid={tooLong || undefined}
                aria-describedby="org-name-hint"
                onChange={(e) => { setDraft(e.target.value); setSaved(null); }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && isAdmin && !problem && !unchanged) save.mutate(trimmed);
                }}
              />
              <em className="hint" id="org-name-hint">
                {tooLong ? `${trimmed.length} of ${max} characters - shorten it to save.`
                  : sourceLine(data)}
              </em>
            </label>
          </div>

          {isAdmin && (
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
              <button className="primary"
                      disabled={!!problem || unchanged || save.isPending}
                      onClick={() => save.mutate(trimmed)}>
                {save.isPending ? 'Saving…' : 'Save name'}
              </button>
              {data.saved_name && (
                <button disabled={save.isPending} onClick={() => save.mutate(null)}>
                  Use server default
                </button>
              )}
              {problem && draft !== current && <span className="muted">{problem}</span>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Where the name shown right now comes from, in words an admin can act on. */
function sourceLine(d: OrganisationSetting): string {
  if (d.source === 'settings') {
    const who = d.updated_by ? ` by ${d.updated_by}` : '';
    const fallback = d.environment_name ?? d.default_name;
    return `Saved here${who}, ${relativeTime(d.updated_at)}. Using the server default would show “${fallback}”.`;
  }
  if (d.source === 'environment') {
    return "Currently set by the server's DCIM_ORG_NAME. A name saved here takes its place.";
  }
  return `Not set. Pages show “${d.default_name}” until a name is saved.`;
}

function errText(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 403) return 'Only an admin can change the organisation name.';
    return e.message;
  }
  return String(e);
}
