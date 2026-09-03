import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, api, type MaintenanceWindow, type Tag } from '../../../api/client';
import { Dialog, DialogActions } from '../components/Dialog';

/** Record work done on one asset.
 *
 *  Separate from the window on purpose. Emergency work has a record and no
 *  window, and a window can end with nothing done - so the record stands on its
 *  own and names a window only when there was one.
 */
export function RecordWorkForm({ deviceId, onClose }: {
  deviceId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [kind, setKind] = useState('corrective');
  const [summary, setSummary] = useState('');
  const [detail, setDetail] = useState('');
  const [windowId, setWindowId] = useState('');
  const [error, setError] = useState<string | null>(null);

  const { data: windows } = useQuery<{ items: MaintenanceWindow[] }>({
    queryKey: ['maintenance-windows', 'device', deviceId],
    queryFn: () => api.maintenanceWindows({ device_id: deviceId }),
  });

  const save = useMutation({
    mutationFn: () => api.createRecord(deviceId, {
      kind,
      summary,
      detail: detail || undefined,
      window_id: windowId || undefined,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['maintenance-records', deviceId] });
      onClose();
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  });

  return (
    <Dialog title="Record work" onClose={onClose}>
      <div className="asset-form">
        <label>
          <span>Kind</span>
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="corrective">Corrective — something was wrong</option>
            <option value="preventive">Preventive — scheduled upkeep</option>
            <option value="firmware">Firmware</option>
            <option value="replacement">Replacement</option>
          </select>
        </label>
        <label>
          <span>Against a window</span>
          <select value={windowId} onChange={(e) => setWindowId(e.target.value)}>
            <option value="">Unplanned — no window</option>
            {(windows?.items ?? []).map((w) => (
              <option key={w.id} value={w.id}>{w.title}</option>
            ))}
          </select>
        </label>
        <label className="asset-form-wide">
          <span>Summary</span>
          <input value={summary} autoFocus
                 placeholder="PSU 2 replaced, both feeds verified"
                 onChange={(e) => setSummary(e.target.value)} />
        </label>
        <label className="asset-form-wide">
          <span>Detail</span>
          <textarea rows={3} value={detail}
                    onChange={(e) => setDetail(e.target.value)} />
        </label>
      </div>
      <p className="asset-form-note">
        Parts consumed will be recorded here once stock has a table of its own.
        Until then, note them in the detail.
      </p>
      {error && <div className="banner">{error}</div>}
      <DialogActions>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" disabled={!summary.trim() || save.isPending}
                onClick={() => { setError(null); save.mutate(); }}>
          {save.isPending ? 'Saving…' : 'Save record'}
        </button>
      </DialogActions>
    </Dialog>
  );
}

/** Attach and detach tags on one object. */
export function TagAssignForm({ objectType, objectId, onClose }: {
  objectType: string;
  objectId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const { data: all } = useQuery<{ items: Tag[] }>({
    queryKey: ['tags'],
    queryFn: api.tags,
  });
  const { data: current } = useQuery<{ items: Tag[] }>({
    queryKey: ['object-tags', objectType, objectId],
    queryFn: () => api.objectTags(objectType, objectId),
  });

  const attached = new Set((current?.items ?? []).map((t) => t.id));

  const toggle = useMutation({
    mutationFn: async (tag: Tag): Promise<void> => {
      if (attached.has(tag.id)) {
        await api.unassignTag(objectType, objectId, tag.id);
      } else {
        await api.assignTags(objectType, objectId, [tag.id]);
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['object-tags', objectType, objectId] });
      qc.invalidateQueries({ queryKey: ['device', objectId] });
      qc.invalidateQueries({ queryKey: ['asset-devices'] });
      qc.invalidateQueries({ queryKey: ['tags'] });
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  });

  const items = all?.items ?? [];

  return (
    <Dialog title="Tags" onClose={onClose}>
      {items.length === 0 ? (
        <p className="muted">
          No tags are defined yet. Create them under Tags, then attach them
          here — the vocabulary is deliberately shared rather than typed in per
          asset.
        </p>
      ) : (
        <div className="asset-picker-list">
          {items.map((t) => (
            <label className="asset-check" key={t.id}>
              <input
                type="checkbox"
                checked={attached.has(t.id)}
                disabled={toggle.isPending}
                onChange={() => toggle.mutate(t)}
              />
              <span className="asset-chip"
                    style={t.colour ? { borderColor: t.colour } : undefined}>
                {t.key}={t.value}
              </span>
              <span className="n">{t.usage_count}</span>
            </label>
          ))}
        </div>
      )}
      {error && <div className="banner">{error}</div>}
      <DialogActions>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Done</button>
      </DialogActions>
    </Dialog>
  );
}
