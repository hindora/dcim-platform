import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { ApiError, api, type MaintenancePreview } from '../../../api/client';
import { Dialog, DialogActions } from '../components/Dialog';
import { DevicePicker } from '../components/DevicePicker';

/** Scheduling a window, in three steps, because step two is the point.
 *
 *  A window that is scoped too widely is otherwise discovered at 02:00 - by
 *  which time it has silenced a rack nobody was working on. The preview asks
 *  the impact graph and the power chain what this selection would actually
 *  cover and says so before anybody commits.
 */
export function WindowForm({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [error, setError] = useState<string | null>(null);

  const [title, setTitle] = useState('');
  const [changeRef, setChangeRef] = useState('');
  const [description, setDescription] = useState('');
  const [kind, setKind] = useState('planned');
  const [suppress, setSuppress] = useState(true);
  const [startsAt, setStartsAt] = useState(defaultStart());
  const [endsAt, setEndsAt] = useState(defaultEnd());
  const [deviceIds, setDeviceIds] = useState<string[]>([]);

  const { data: preview, isFetching } = useQuery<MaintenancePreview>({
    queryKey: ['maintenance-preview', deviceIds.join(',')],
    queryFn: () => api.maintenancePreview(deviceIds),
    enabled: step === 2 && deviceIds.length > 0,
  });

  const create = useMutation({
    mutationFn: () => api.createWindow({
      title,
      description: description || undefined,
      change_ref: changeRef || undefined,
      kind,
      starts_at: new Date(startsAt).toISOString(),
      ends_at: new Date(endsAt).toISOString(),
      suppress,
      device_ids: deviceIds,
    }),
    onSuccess: (window) => {
      qc.invalidateQueries({ queryKey: ['maintenance-windows'] });
      onClose();
      navigate(`/assets/maintenance/${window.id}`);
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  });

  const datesValid = Boolean(startsAt && endsAt
    && new Date(endsAt) > new Date(startsAt));

  return (
    <Dialog title="Schedule maintenance" onClose={onClose} wide={step === 2}>
      <ol className="asset-steps">
        <li className={step === 1 ? 'active' : ''}>When</li>
        <li className={step === 2 ? 'active' : ''}>Scope</li>
        <li className={step === 3 ? 'active' : ''}>Confirm</li>
      </ol>

      {step === 1 && (
        <div className="asset-form">
          <label>
            <span>Title</span>
            <input value={title} onChange={(e) => setTitle(e.target.value)}
                   placeholder="CRAH 3 filter change" autoFocus />
          </label>
          <label>
            <span>Change reference</span>
            <input value={changeRef} onChange={(e) => setChangeRef(e.target.value)}
                   placeholder="CHG-…" />
          </label>
          <label>
            <span>Starts</span>
            <input type="datetime-local" value={startsAt}
                   onChange={(e) => setStartsAt(e.target.value)} />
          </label>
          <label>
            <span>Ends</span>
            <input type="datetime-local" value={endsAt}
                   onChange={(e) => setEndsAt(e.target.value)} />
          </label>
          <label>
            <span>Kind</span>
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="planned">Planned</option>
              <option value="emergency">Emergency</option>
            </select>
          </label>
          <label className="asset-form-wide">
            <span>Description</span>
            <textarea rows={2} value={description}
                      onChange={(e) => setDescription(e.target.value)} />
          </label>
          <label className="asset-form-wide asset-check">
            <input type="checkbox" checked={suppress}
                   onChange={(e) => setSuppress(e.target.checked)} />
            <span>
              Hold back alarms on these devices while the window runs.
              {!suppress && ' Off: this is a calendar entry and silences nothing.'}
            </span>
          </label>
          {!datesValid && (startsAt || endsAt) && (
            <p className="asset-form-error asset-form-wide">
              A window has to end after it starts.
            </p>
          )}
        </div>
      )}

      {step === 2 && (
        <>
          <DevicePicker selected={deviceIds} onChange={setDeviceIds} />
          <div className="asset-preview">
            {deviceIds.length === 0 && (
              <p className="muted">
                Select the devices being worked on. A window with no targets
                silences nothing.
              </p>
            )}
            {deviceIds.length > 0 && isFetching && (
              <p className="muted">Working out what this would cover…</p>
            )}
            {preview && !isFetching && (
              <>
                <div className="asset-preview-row">
                  <Stat n={preview.devices} label="selected" />
                  <Stat n={preview.downstream_devices} label="downstream"
                        tone={preview.downstream_devices ? 'warn' : undefined} />
                  <Stat n={preview.cut_off} label="would go dark"
                        tone={preview.cut_off ? 'bad' : undefined} />
                  <Stat n={preview.alarms_currently_active} label="alarms standing" />
                </div>
                {preview.redundancy_warnings.length > 0 && (
                  <div className="asset-preview-warn">
                    <strong>
                      {preview.redundancy_warnings.length} of these are not
                      redundantly fed
                    </strong>
                    <ul>
                      {preview.redundancy_warnings.slice(0, 6).map((w) => (
                        <li key={w.device_id}>{w.reason}</li>
                      ))}
                    </ul>
                    Taking a feeder into a window costs these their power, not
                    just their alarms.
                  </div>
                )}
              </>
            )}
          </div>
        </>
      )}

      {step === 3 && (
        <div className="asset-confirm">
          <p>
            <strong>{title || 'Untitled window'}</strong> over{' '}
            <strong>{deviceIds.length}</strong> device
            {deviceIds.length === 1 ? '' : 's'}, from {fmt(startsAt)} to{' '}
            {fmt(endsAt)}.
          </p>
          <p className="muted">
            {suppress
              ? 'Alarms raised on these devices while it runs will be recorded '
                + 'and held out of the active list. Anything still open when it '
                + 'ends comes back automatically.'
              : 'This window will not hold back any alarms.'}
          </p>
          {preview && preview.cut_off > 0 && (
            <p className="asset-form-error">
              {preview.cut_off} devices downstream would lose power entirely.
            </p>
          )}
          {error && <div className="banner">{error}</div>}
        </div>
      )}

      <DialogActions>
        {step > 1 && (
          <button type="button" onClick={() => setStep((s) => (s - 1) as 1 | 2)}>
            Back
          </button>
        )}
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        {step < 3 ? (
          <button
            type="button"
            disabled={step === 1 && (!title.trim() || !datesValid)}
            onClick={() => setStep((s) => (s + 1) as 2 | 3)}
          >
            Continue
          </button>
        ) : (
          <button type="button" disabled={create.isPending}
                  onClick={() => { setError(null); create.mutate(); }}>
            {create.isPending ? 'Scheduling…' : 'Schedule window'}
          </button>
        )}
      </DialogActions>
    </Dialog>
  );
}

function Stat({ n, label, tone }: { n: number; label: string; tone?: string }) {
  return (
    <div className={`asset-stat${tone ? ` is-${tone}` : ''}`}>
      <div className="v">{n}</div>
      <div className="k">{label}</div>
    </div>
  );
}

/** `datetime-local` wants local wall-clock with no zone, which is also what an
 *  operator types. It is converted to an instant on submit. */
function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
    + `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function defaultStart(): string {
  const d = new Date();
  d.setMinutes(0, 0, 0);
  d.setHours(d.getHours() + 1);
  return toLocalInput(d);
}

function defaultEnd(): string {
  const d = new Date();
  d.setMinutes(0, 0, 0);
  d.setHours(d.getHours() + 3);
  return toLocalInput(d);
}

function fmt(local: string): string {
  if (!local) return '—';
  return new Date(local).toLocaleString(undefined, {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  });
}
