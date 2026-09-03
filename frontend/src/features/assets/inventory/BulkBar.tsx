import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ApiError,
  api,
  type BulkReport,
  type Tag,
} from '../../../api/client';
import { humanise } from '../../../lib/format';
import { Dialog, DialogActions } from '../components/Dialog';

/** The table's toolbar, which becomes contextual when rows are selected.
 *
 *  ABOVE the table, not pinned to the bottom of the viewport. A bottom bar sits
 *  a screen away from the checkboxes that summon it, competes with the pager
 *  for the same strip, and is a thumb-reach pattern borrowed from mobile. The
 *  actions belong beside the header checkbox that selects everything.
 *
 *  It replaces the idle toolbar rather than appearing beneath it, so nothing on
 *  the page moves when a selection starts - a row jumping out from under the
 *  pointer mid-click is how somebody ticks the wrong asset.
 *
 *  Two rules run through the actions themselves. Each states what it will do to
 *  how many things BEFORE doing it, and each result is shown row by row - "2
 *  failed" in a toast is how a feature stops being trusted, because the
 *  operator cannot tell which two, retry them, or find out why.
 */

type Action = 'lifecycle' | 'tags' | 'fields' | null;

export function BulkBar({ selected, onClear, allowed, children }: {
  selected: string[];
  onClear: () => void;
  /** Transitions the server says every selected asset may make. */
  allowed: string[];
  /** The idle toolbar - export, import. Shown when nothing is selected, so one
   *  strip serves both states and the layout never shifts. */
  children?: React.ReactNode;
}) {
  const [action, setAction] = useState<Action>(null);
  const [report, setReport] = useState<BulkReport | null>(null);

  // Escape clears a selection. Standard for a contextual mode, and it is the
  // fastest way out when somebody has ticked forty rows by mistake.
  useEffect(() => {
    if (selected.length === 0) return undefined;
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape' && !action) onClear();
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [selected.length, action, onClear]);

  if (selected.length === 0 && !report) return null;

  const active = selected.length > 0;

  return (
    <>
      <div className={`asset-toolbar${active ? ' is-selecting' : ''}`}>
        {active ? (
          <>
            {/* Announced, not just drawn: a screen reader has no other way to
                know the toolbar changed under it. */}
            <strong aria-live="polite">
              {selected.length} selected
            </strong>
            <span style={{ flex: 1 }} />
            <button type="button" onClick={() => setAction('lifecycle')}>
              Change lifecycle
            </button>
            <button type="button" onClick={() => setAction('tags')}>Tags</button>
            <button type="button" onClick={() => setAction('fields')}>
              Set ownership
            </button>
            <button type="button" className="asset-toolbar-clear"
                    onClick={onClear} aria-label="Clear selection"
                    title="Clear selection (Esc)">
              ✕
            </button>
          </>
        ) : (
          children
        )}
      </div>

      {action === 'lifecycle' && (
        <LifecycleAction selected={selected} allowed={allowed}
                         onClose={() => setAction(null)} onDone={setReport} />
      )}
      {action === 'tags' && (
        <TagsAction selected={selected} onClose={() => setAction(null)}
                    onDone={setReport} />
      )}
      {action === 'fields' && (
        <FieldsAction selected={selected} onClose={() => setAction(null)}
                      onDone={setReport} />
      )}

      {report && <ReportView report={report} onClose={() => setReport(null)} />}
    </>
  );
}

/** The result, as a view rather than a notification.
 *
 *  Failures carry the object's name and a sentence written for a person, so the
 *  operator can act on them — and can copy the lot into the ticket that will
 *  ask why.
 */
function ReportView({ report, onClose }: {
  report: BulkReport; onClose: () => void;
}) {
  const failed = report.failed.length;
  return (
    <Dialog title={failed ? 'Partly applied' : 'Done'} onClose={onClose} wide={failed > 0}>
      <p>
        <strong>{report.succeeded}</strong> applied
        {failed > 0 && <>, <strong>{failed}</strong> refused</>}.
      </p>

      {failed > 0 && (
        <>
          <div className="asset-scroll">
            <table>
              <thead>
                <tr><th>Asset</th><th>Why</th></tr>
              </thead>
              <tbody>
                {report.failed.map((f) => (
                  <tr key={f.device_id}>
                    <td>{f.name ?? <span className="asset-tag">{f.device_id}</span>}</td>
                    <td>
                      {f.message}
                      <div className="muted asset-tag" style={{ fontSize: '0.72rem' }}>
                        {f.error}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      <DialogActions>
        {failed > 0 && (
          <button
            type="button"
            onClick={() => navigator.clipboard?.writeText(
              report.failed
                .map((f) => `${f.name ?? f.device_id}\t${f.error}\t${f.message}`)
                .join('\n'))}
          >
            Copy report
          </button>
        )}
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Close</button>
      </DialogActions>
    </Dialog>
  );
}

function LifecycleAction({ selected, allowed, onClose, onDone }: {
  selected: string[]; allowed: string[];
  onClose: () => void; onDone: (r: BulkReport) => void;
}) {
  const qc = useQueryClient();
  const [state, setState] = useState('');
  const [reason, setReason] = useState('');
  const [changeRef, setChangeRef] = useState('');
  const [error, setError] = useState<string | null>(null);

  const run = useMutation({
    mutationFn: () => api.bulkLifecycle({
      device_ids: selected, to_state: state,
      reason: reason || undefined, change_ref: changeRef || undefined,
    }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ['asset-devices'] });
      qc.invalidateQueries({ queryKey: ['asset-summary'] });
      onClose();
      onDone(r);
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  });

  return (
    <Dialog title="Change lifecycle" onClose={onClose}>
      <div className="asset-form">
        <label>
          <span>New state</span>
          <select value={state} autoFocus onChange={(e) => setState(e.target.value)}>
            <option value="">Choose…</option>
            {allowed.map((s) => (
              <option key={s} value={s}>{humanise(s)}</option>
            ))}
          </select>
        </label>
        <label>
          <span>Change reference</span>
          <input value={changeRef} placeholder="CHG-…"
                 onChange={(e) => setChangeRef(e.target.value)} />
        </label>
        <label className="asset-form-wide">
          <span>Reason</span>
          <input value={reason} onChange={(e) => setReason(e.target.value)}
                 placeholder="Recorded against every one of them" />
        </label>
      </div>

      {/* Say what will happen, to how many, before it happens. */}
      <p className="asset-form-note">
        This will move <strong>{selected.length}</strong> asset
        {selected.length === 1 ? '' : 's'} to{' '}
        <strong>{state ? humanise(state) : '…'}</strong>, one at a time. Any that
        cannot make that transition are reported and the rest still apply.
      </p>

      {allowed.length === 0 && (
        <p className="asset-form-error">
          The selection has no transition in common. Narrow it to assets in the
          same state.
        </p>
      )}
      {error && <div className="banner">{error}</div>}

      <DialogActions>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" disabled={!state || run.isPending}
                onClick={() => { setError(null); run.mutate(); }}>
          {run.isPending ? 'Applying…' : `Apply to ${selected.length}`}
        </button>
      </DialogActions>
    </Dialog>
  );
}

function TagsAction({ selected, onClose, onDone }: {
  selected: string[]; onClose: () => void; onDone: (r: BulkReport) => void;
}) {
  const qc = useQueryClient();
  const [add, setAdd] = useState<string[]>([]);
  const [remove, setRemove] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const { data } = useQuery<{ items: Tag[] }>({
    queryKey: ['tags'], queryFn: api.tags,
  });

  const run = useMutation({
    mutationFn: () => api.bulkTags({ device_ids: selected, add, remove }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ['asset-devices'] });
      qc.invalidateQueries({ queryKey: ['tags'] });
      onClose();
      onDone(r);
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  });

  function cycle(id: string) {
    // Three states per tag: leave alone, attach, detach. A checkbox cannot say
    // "remove this from all of them", which is half of what bulk tagging is for.
    if (add.includes(id)) {
      setAdd(add.filter((x) => x !== id));
      setRemove([...remove, id]);
    } else if (remove.includes(id)) {
      setRemove(remove.filter((x) => x !== id));
    } else {
      setAdd([...add, id]);
    }
  }

  const items = data?.items ?? [];

  return (
    <Dialog title="Tags" onClose={onClose}>
      {items.length === 0 ? (
        <p className="muted">No tags defined yet.</p>
      ) : (
        <>
          <p className="muted">
            Click to attach, again to detach, again to leave unchanged.
          </p>
          <div className="asset-picker-list">
            {items.map((t) => {
              const mark = add.includes(t.id) ? 'add'
                : remove.includes(t.id) ? 'remove' : '';
              return (
                <button type="button" key={t.id}
                        className={`asset-tagpick is-${mark || 'none'}`}
                        onClick={() => cycle(t.id)}>
                  <span className="asset-chip"
                        style={t.colour ? { borderColor: t.colour } : undefined}>
                    {t.key}={t.value}
                  </span>
                  <span className="n">
                    {mark === 'add' ? 'attach' : mark === 'remove' ? 'detach' : ''}
                  </span>
                </button>
              );
            })}
          </div>
        </>
      )}

      <p className="asset-form-note">
        {add.length + remove.length === 0
          ? 'Nothing chosen yet.'
          : `Across ${selected.length} assets: `
            + [add.length && `attach ${add.length}`,
               remove.length && `detach ${remove.length}`]
              .filter(Boolean).join(', ') + '.'}
      </p>
      {error && <div className="banner">{error}</div>}

      <DialogActions>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button"
                disabled={(add.length + remove.length) === 0 || run.isPending}
                onClick={() => { setError(null); run.mutate(); }}>
          {run.isPending ? 'Applying…' : `Apply to ${selected.length}`}
        </button>
      </DialogActions>
    </Dialog>
  );
}

function FieldsAction({ selected, onClose, onDone }: {
  selected: string[]; onClose: () => void; onDone: (r: BulkReport) => void;
}) {
  const qc = useQueryClient();
  const [owner, setOwner] = useState('');
  const [costCentre, setCostCentre] = useState('');
  const [error, setError] = useState<string | null>(null);

  // Only what was actually typed. An empty box means "leave it alone", not
  // "blank it on four hundred assets".
  const changes: Record<string, string> = {};
  if (owner.trim()) changes.owner_group = owner.trim();
  if (costCentre.trim()) changes.cost_centre = costCentre.trim();

  const run = useMutation({
    mutationFn: () => api.bulkFields({ device_ids: selected, set: changes }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ['asset-devices'] });
      onClose();
      onDone(r);
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  });

  return (
    <Dialog title="Set ownership" onClose={onClose}>
      <div className="asset-form">
        <label>
          <span>Owner group</span>
          <input value={owner} autoFocus onChange={(e) => setOwner(e.target.value)} />
        </label>
        <label>
          <span>Cost centre</span>
          <input value={costCentre}
                 onChange={(e) => setCostCentre(e.target.value)} />
        </label>
      </div>
      <p className="asset-form-note">
        Empty fields are left unchanged.
      </p>
      {error && <div className="banner">{error}</div>}
      <DialogActions>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button"
                disabled={Object.keys(changes).length === 0 || run.isPending}
                onClick={() => { setError(null); run.mutate(); }}>
          {run.isPending ? 'Applying…' : `Apply to ${selected.length}`}
        </button>
      </DialogActions>
    </Dialog>
  );
}
