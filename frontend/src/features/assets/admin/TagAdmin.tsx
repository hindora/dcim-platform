import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, api, type Tag } from '../../../api/client';
import { Dialog, DialogActions } from '../components/Dialog';

/** The controlled vocabulary.
 *
 *  key/value rather than flat labels, so `env=prod` and `env=dev` are one
 *  dimension with two values. A free text box collects `Prod`, `prod` and
 *  `production` and then nobody can filter on any of them.
 */
export function TagAdmin() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<Tag | 'new' | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Tag | null>(null);

  const { data, isLoading } = useQuery<{ items: Tag[] }>({
    queryKey: ['tags'],
    queryFn: api.tags,
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteTag(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tags'] });
      qc.invalidateQueries({ queryKey: ['asset-devices'] });
      setConfirmDelete(null);
    },
  });

  const items = data?.items ?? [];
  const byKey = new Map<string, Tag[]>();
  for (const tag of items) {
    byKey.set(tag.key, [...(byKey.get(tag.key) ?? []), tag]);
  }

  return (
    <>
      <h2>Tags</h2>
      <p className="asset-table-note">
        <button type="button" onClick={() => setEditing('new')}>New tag</button>
      </p>

      {isLoading && <p className="muted">Loading…</p>}

      {!isLoading && items.length === 0 && (
        <div className="asset-empty">
          No tags defined.
        </div>
      )}

      {[...byKey.entries()].map(([key, tags]) => (
        <section key={key} style={{ marginBottom: 20 }}>
          <h3>{key}</h3>
          <div className="asset-scroll">
            <table>
              <thead>
                <tr><th>Value</th><th>Description</th><th>Used by</th><th /></tr>
              </thead>
              <tbody>
                {tags.map((t) => (
                  <tr key={t.id}>
                    <td>
                      <span className="asset-chip"
                            style={t.colour ? { borderColor: t.colour } : undefined}>
                        {t.key}={t.value}
                      </span>
                    </td>
                    <td className="muted">
                      {t.description ?? <span className="asset-none">—</span>}
                    </td>
                    <td className="muted">{t.usage_count}</td>
                    <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                      <button type="button" onClick={() => setEditing(t)}>Edit</button>
                      {' '}
                      <button type="button" onClick={() => setConfirmDelete(t)}>
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ))}

      {editing && (
        <TagForm
          tag={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
        />
      )}

      {confirmDelete && (
        <Dialog title="Delete tag" onClose={() => setConfirmDelete(null)}>
          <p>
            Delete <strong>{confirmDelete.key}={confirmDelete.value}</strong>?
          </p>
          <p className="muted">
            {confirmDelete.usage_count > 0
              ? `${confirmDelete.usage_count} objects lose this label.`
              : 'Nothing is using it.'}
          </p>
          <DialogActions>
            <button type="button" disabled={remove.isPending}
                    onClick={() => remove.mutate(confirmDelete.id)}>
              {remove.isPending ? 'Deleting…' : 'Delete'}
            </button>
            <span style={{ flex: 1 }} />
            <button type="button" onClick={() => setConfirmDelete(null)}>
              Keep it
            </button>
          </DialogActions>
        </Dialog>
      )}
    </>
  );
}

function TagForm({ tag, onClose }: { tag: Tag | null; onClose: () => void }) {
  const qc = useQueryClient();
  const [key, setKey] = useState(tag?.key ?? '');
  const [value, setValue] = useState(tag?.value ?? '');
  const [colour, setColour] = useState(tag?.colour ?? '');
  const [description, setDescription] = useState(tag?.description ?? '');
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: async (): Promise<void> => {
      if (tag) {
        await api.patchTag(tag.id, { key, value, colour: colour || null,
                                     description: description || null });
      } else {
        await api.createTag({ key, value, colour: colour || undefined,
                              description: description || undefined });
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tags'] });
      qc.invalidateQueries({ queryKey: ['asset-devices'] });
      onClose();
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  });

  return (
    <Dialog title={tag ? 'Edit tag' : 'New tag'} onClose={onClose}>
      <div className="asset-form">
        <label>
          <span>Key</span>
          <input value={key} autoFocus placeholder="env"
                 onChange={(e) => setKey(e.target.value)} />
        </label>
        <label>
          <span>Value</span>
          <input value={value} placeholder="prod"
                 onChange={(e) => setValue(e.target.value)} />
        </label>
        <label>
          <span>Colour</span>
          <input type="color" value={colour || '#8b949e'}
                 onChange={(e) => setColour(e.target.value)} />
        </label>
        <label className="asset-form-wide">
          <span>Description</span>
          <input value={description}
                 onChange={(e) => setDescription(e.target.value)} />
        </label>
      </div>
      {tag && tag.usage_count > 0 && (
        <p className="asset-form-note">
          Renaming this changes it on {tag.usage_count} objects at once — which
          is the point of a vocabulary, and worth knowing before you do it.
        </p>
      )}
      {error && <div className="banner">{error}</div>}
      <DialogActions>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button"
                disabled={!key.trim() || !value.trim() || save.isPending}
                onClick={() => { setError(null); save.mutate(); }}>
          {save.isPending ? 'Saving…' : 'Save'}
        </button>
      </DialogActions>
    </Dialog>
  );
}
