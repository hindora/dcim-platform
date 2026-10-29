import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { api, ApiError, atLeast, type DeviceDetail } from '../../api/client';
import { NumberInput } from '../../components/NumberInput';

/** The model's power budget, the figure the Power page's Allocated column
 *  is built from, with its basis beside it - and, for an admin, the means
 *  to set or clear it.
 *
 *  One budget per MODEL: a planner commits to an R640 once and every R640
 *  inherits it, which is why this edits the model and says so. A cleared
 *  budget hands the model back to the planning rule, rated power times the
 *  derate, and the fact says that too rather than showing a dash. */
export function ModelBudget({ d }: { d: DeviceDetail }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [watts, setWatts] = useState<number>(d.budget_w ?? d.rated_power_w ?? 0);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const rated = d.rated_power_w ?? null;
  const derate = d.budget_derate ?? 0.6;
  const ruleW = rated != null ? Math.round(rated * derate) : null;
  const canEdit = atLeast('admin') && !!d.model_id;

  async function save(clear: boolean) {
    if (!d.model_id) return;
    setBusy(true); setErr(null);
    try {
      await api.setModelBudget(d.model_id, clear ? { budget_w: null } : { budget_w: watts, note: note || undefined });
      await qc.invalidateQueries({ queryKey: ['device', d.id] });
      setEditing(false);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'could not save the budget');
    } finally {
      setBusy(false);
    }
  }

  const value = d.budget_w != null
    ? <>{d.budget_w} W<span className="muted"> · {d.budget_source ?? 'no basis recorded'}</span></>
    : ruleW != null
      ? <>{ruleW} W<span className="muted"> · planning rule, {Math.round(derate * 100)} % of the {rated} W rating; no budget recorded</span></>
      : <span className="muted">no rating and no budget: this device is not counted in Allocated</span>;

  return (
    <div className="dv-fact dv-budget">
      <div className="k">Budgeted <span className="muted">(per model)</span></div>
      <div className="v">
        {value}
        {canEdit && !editing && (
          <button type="button" className="dv-link" onClick={() => { setWatts(d.budget_w ?? d.rated_power_w ?? 0); setEditing(true); }}>
            {d.budget_w != null ? 'Change' : 'Set budget'}
          </button>
        )}
      </div>
      {editing && (
        <form className="dv-budget-form" onSubmit={(e) => { e.preventDefault(); void save(false); }}>
          <label>
            Watts per {d.model ?? 'unit'}
            <NumberInput value={watts} onChange={setWatts} min={0} step={10} />
          </label>
          <label>
            Basis
            <input type="text" value={note} maxLength={200} placeholder="data sheet typical, measured peak, policy…"
                   onChange={(e) => setNote(e.target.value)} />
          </label>
          <div className="dv-budget-actions">
            <button type="submit" disabled={busy}>Save</button>
            {d.budget_w != null && (
              <button type="button" disabled={busy} onClick={() => void save(true)}
                      title={ruleW != null ? `Back to the planning rule: ${ruleW} W` : 'Back to the planning rule'}>
                Clear
              </button>
            )}
            <button type="button" disabled={busy} onClick={() => setEditing(false)}>Cancel</button>
          </div>
          <p className="muted">Applies to every {d.model ?? 'unit of this model'} in the estate. The Power page reads it on its next refresh. Recorded as yours, with today's date; a re-import leaves it alone.</p>
          {err && <p className="dv-err">{err}</p>}
        </form>
      )}
    </div>
  );
}
