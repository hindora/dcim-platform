import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  api,
  type DiscoveryRange,
  type DiscoverySchedule,
  type RangeOptions,
} from '../../../api/client';
import { Dialog, DialogActions } from '../components/Dialog';

export const INTERVAL_LABEL: Record<number, string> = {
  6: 'every 6 hours', 12: 'every 12 hours', 24: 'every 24 hours', 48: 'every 2 days',
  168: 'every week',
};
export const intervalLabel = (h: number) => INTERVAL_LABEL[h] ?? `every ${h} hours`;

const DAY = ['', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const DAY_LONG = ['', 'Mondays', 'Tuesdays', 'Wednesdays', 'Thursdays', 'Fridays',
                  'Saturdays', 'Sundays'];
const WEEKDAYS = [1, 2, 3, 4, 5];
const EVERY_DAY = [1, 2, 3, 4, 5, 6, 7];

/** "weekdays at 02:00 Asia/Kolkata", "every 24 hours". */
export function timingLabel(s: Pick<DiscoverySchedule,
  'run_at' | 'days' | 'timezone' | 'interval_hours'>): string {
  if (!s.run_at) return intervalLabel(s.interval_hours);
  const days = [...(s.days ?? EVERY_DAY)].sort();
  const when = days.length === 7 ? 'daily'
    : days.join() === WEEKDAYS.join() ? 'weekdays'
      : days.length === 1 ? DAY_LONG[days[0]]
        : days.map((d) => DAY[d]).join(', ');
  return `${when} at ${s.run_at} ${s.timezone ?? ''}`.trim();
}

/** A tag's worth: "daily", "weekdays", "12h", "weekly". */
export function shortTiming(s: Pick<DiscoverySchedule,
  'run_at' | 'days' | 'interval_hours'>): string {
  if (s.run_at) {
    const days = [...(s.days ?? EVERY_DAY)].sort();
    if (days.length === 7) return 'daily';
    if (days.join() === WEEKDAYS.join()) return 'weekdays';
    if (days.length === 1) return 'weekly';
    return `${days.length}d/wk`;
  }
  const h = s.interval_hours;
  return h === 168 ? 'weekly' : h % 24 === 0 ? `${h / 24}d` : `${h}h`;
}

/** A time in a zone, as its local clock reads it: "Tue 02:00". */
export function inZone(iso: string, tz?: string | null): string {
  try {
    return new Intl.DateTimeFormat(undefined, {
      weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
      timeZone: tz ?? undefined,
    }).format(new Date(iso));
  } catch {
    return new Date(iso).toLocaleString();
  }
}

type When = 'interval' | 'daily' | 'weekdays' | 'weekly';

function whenOf(s?: DiscoverySchedule): When {
  if (!s?.run_at) return 'interval';
  const days = [...(s.days ?? [])].sort().join();
  if (days === EVERY_DAY.join()) return 'daily';
  if (days === WEEKDAYS.join()) return 'weekdays';
  return 'weekly';
}

/** Create or edit a schedule: name, when, and which saved ranges.
 *
 *  "When" is either an interval from the last run or a local time on chosen days
 *  in a site's timezone - the quiet window a site actually sweeps in. The zone
 *  defaults to the selected ranges' site, because 02:00 is a local fact.
 */
export function ScheduleDialog({ schedule, initialRangeIds, ranges, intervals, options,
                                 onClose }: {
  /** Editing this one; absent to create. */
  schedule?: DiscoverySchedule;
  /** Pre-ticked when creating, e.g. the ranges selected in the sweep table. */
  initialRangeIds?: string[];
  ranges: DiscoveryRange[];
  intervals: number[];
  options?: RangeOptions;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(schedule?.name ?? '');
  const [when, setWhen] = useState<When>(whenOf(schedule));
  const [hours, setHours] = useState(schedule && !schedule.run_at
    ? schedule.interval_hours : 24);
  const [runAt, setRunAt] = useState(schedule?.run_at ?? '02:00');
  const [weekday, setWeekday] = useState(
    schedule?.run_at && (schedule.days ?? []).length === 1 ? schedule.days![0] : 7);
  const [picked, setPicked] = useState<Set<string>>(
    new Set(schedule?.range_ids ?? initialRangeIds ?? []));
  const chosen = ranges.filter((r) => picked.has(r.id));

  // The zones on offer: each site's, UTC, and this browser's. The default is the
  // chosen ranges' site when they agree on one.
  const siteZone = (r: DiscoveryRange) =>
    options?.datacenters.find((d) => d.id === r.datacenter_id)?.timezone ?? null;
  const chosenZones = [...new Set(chosen.map(siteZone).filter(Boolean))] as string[];
  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const zones = [...new Set([
    ...(options?.datacenters ?? []).map((d) => d.timezone).filter(Boolean) as string[],
    'UTC', browserZone, schedule?.timezone ?? '',
  ].filter(Boolean))];
  const [tz, setTz] = useState(schedule?.timezone
    ?? (chosenZones.length === 1 ? chosenZones[0] : 'UTC'));
  const [error, setError] = useState<string | null>(null);

  const toggle = (id: string) => setPicked((s) => {
    const next = new Set(s);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const days = when === 'daily' ? EVERY_DAY : when === 'weekdays' ? WEEKDAYS : [weekday];
  const timed = when !== 'interval';
  const badTime = timed && !/^([01]\d|2[0-3]):[0-5]\d$/.test(runAt);

  const save = useMutation({
    mutationFn: () => {
      const base = {
        // Blank means "name it after its ranges", as the API does on create.
        name: name.trim() || chosen.map((r) => r.name).join(', '),
        range_ids: chosen.map((r) => r.id),
      };
      const timing = timed ? { run_at: runAt, days, timezone: tz } : null;
      if (schedule) {
        // run_at: null is what turns a timed schedule back into an interval one.
        return api.updateDiscoverySchedule(schedule.id, {
          ...base,
          ...(timing ?? { interval_hours: hours, run_at: null, days: null, timezone: null }),
        });
      }
      return api.createDiscoverySchedule({ ...base, ...(timing ?? { interval_hours: hours }) });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['discovery-schedules'] });
      qc.invalidateQueries({ queryKey: ['discovery-ranges'] });
      onClose();
    },
    onError: (e) => setError(String(e)),
  });

  return (
    <Dialog title={schedule ? `Edit ${schedule.name || 'schedule'}` : 'New schedule'}
            onClose={onClose}>
      <div className="asset-form">
        <label className="asset-form-wide">
          <span>Name</span>
          <input value={name} onChange={(e) => setName(e.target.value)}
                 placeholder="Named after its ranges" />
        </label>

        <label>
          <span>When</span>
          <select value={when} onChange={(e) => setWhen(e.target.value as When)}>
            <option value="interval">Every few hours</option>
            <option value="daily">Daily at…</option>
            <option value="weekdays">Weekdays at…</option>
            <option value="weekly">Weekly on…</option>
          </select>
        </label>
        {when === 'interval' ? (
          <label>
            <span>Interval</span>
            <select value={hours} onChange={(e) => setHours(Number(e.target.value))}>
              {intervals.map((h) => <option key={h} value={h}>{intervalLabel(h)}</option>)}
            </select>
          </label>
        ) : (
          <label>
            <span>Time</span>
            <input type="time" value={runAt} onChange={(e) => setRunAt(e.target.value)}
                   aria-invalid={badTime} />
          </label>
        )}
        {when === 'weekly' && (
          <label>
            <span>Day</span>
            <select value={weekday} onChange={(e) => setWeekday(Number(e.target.value))}>
              {EVERY_DAY.map((d) => <option key={d} value={d}>{DAY_LONG[d]}</option>)}
            </select>
          </label>
        )}
        {timed && (
          <label>
            <span>Timezone</span>
            <select value={tz} onChange={(e) => setTz(e.target.value)}>
              {zones.map((z) => <option key={z} value={z}>{z}</option>)}
            </select>
          </label>
        )}

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
        {badTime && <p className="disc-bad asset-form-wide">Enter a time, e.g. 02:00.</p>}
        {error && <div className="banner asset-form-wide">{error}</div>}
      </div>
      <DialogActions>
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" className="primary"
                disabled={chosen.length === 0 || badTime || save.isPending}
                onClick={() => { setError(null); save.mutate(); }}>
          {save.isPending ? 'Saving…' : schedule ? 'Save' : 'Create'}
        </button>
      </DialogActions>
    </Dialog>
  );
}
