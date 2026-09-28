import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api, type DiscoveryRange, type DiscoverySchedule } from '../../../api/client';
import { Dialog, DialogActions } from '../components/Dialog';

export const INTERVAL_LABEL: Record<number, string> = {
  6: 'every 6 hours', 12: 'every 12 hours', 24: 'daily', 48: 'every 2 days',
  168: 'weekly',
};
export const intervalLabel = (h: number) => INTERVAL_LABEL[h] ?? `every ${h} hours`;

/** Change a schedule: its name, how often, and which ranges.
 *
 *  Ranges are picked from the saved list, so a schedule always points at ranges
 *  that exist - and editing one of those ranges later changes what this sweeps.
 *  The next run is not moved: an edit is a change of what, not of when.
 */
export function ScheduleDialog({ schedule, ranges, intervals, onClose }: {
  schedule: DiscoverySchedule;
  ranges: DiscoveryRange[];
  intervals: number[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(schedule.name ?? '');
  const [hours, setHours] = useState(schedule.interval_hours);
  const [picked, setPicked] = useState<Set<string>>(new Set(schedule.range_ids));
  const [error, setError] = useState<string | null>(null);

  const toggle = (id: string) => setPicked((s) => {
    const next = new Set(s);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const chosen = ranges.filter((r) => picked.has(r.id));
  const save = useMutation({
    mutationFn: () => api.updateDiscoverySchedule(schedule.id, {
      // Blank means "name it after its ranges", as creating one does.
      name: name.trim() || chosen.map((r) => r.name).join(', '),
      interval_hours: hours,
      range_ids: chosen.map((r) => r.id),
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['discovery-schedules'] });
      onClose();
    },
    onError: (e) => setError(String(e)),
  });

  return (
    <Dialog title={`Edit ${schedule.name || 'schedule'}`} onClose={onClose}>
      <div className="asset-form">
        <label>
          <span>Name</span>
          <input value={name} onChange={(e) => setName(e.target.value)}
                 placeholder="Named after its ranges" />
        </label>
        <label>
          <span>How often</span>
          <select value={hours} onChange={(e) => setHours(Number(e.target.value))}>
            {intervals.map((h) => <option key={h} value={h}>{intervalLabel(h)}</option>)}
          </select>
        </label>

        <fieldset className="asset-form-wide disc-mon-set">
          <legend>Ranges</legend>
          {ranges.length === 0 ? (
            <p className="muted">No saved ranges.</p>
          ) : (
            <div className="disc-sched-ranges">
              {ranges.map((r) => (
                <label key={r.id} className="asset-check">
                  <input type="checkbox" checked={picked.has(r.id)}
                         onChange={() => toggle(r.id)} />
                  <span>
                    {r.name}
                    {r.name !== r.cidr && <code> {r.cidr}</code>}
                    {!r.enabled && <span className="muted"> (disabled - skipped)</span>}
                  </span>
                </label>
              ))}
            </div>
          )}
        </fieldset>
        {chosen.length === 0 && (
          <p className="disc-bad asset-form-wide">Choose at least one range.</p>
        )}
        {error && <div className="banner asset-form-wide">{error}</div>}
      </div>
      <DialogActions>
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" className="primary"
                disabled={chosen.length === 0 || save.isPending}
                onClick={() => { setError(null); save.mutate(); }}>
          {save.isPending ? 'Saving…' : 'Save'}
        </button>
      </DialogActions>
    </Dialog>
  );
}
