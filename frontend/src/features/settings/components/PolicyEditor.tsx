import { useMemo, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import {
  api, type IntegrationConfig, type IntegrationPolicy, type PolicyPreview,
} from '../../../api/client';
import { Seg } from '../../../components/estate';
import { VColumns } from '../../assets/components/Shapes';
import { Tip } from '../../../components/HoverTip';
import { oneLine } from '../../../lib/format';

const SEVERITIES = ['CRITICAL', 'MAJOR', 'MINOR', 'WARNING', 'INFO'] as const;
const CATEGORIES = ['power', 'cooling', 'environmental', 'it_equipment',
  'network', 'capacity', 'visibility'] as const;
const CLASSES = ['alarm', 'alert'] as const;
const RANGES = [7, 14, 30, 90] as const;

/** The clauses that decide which conditions earn a ticket, and a rehearsal.
 *
 *  The rehearsal is the point. A policy is a promise about somebody else's
 *  queue, and the only honest way to edit one is against what actually
 *  happened - so every change re-runs against the last N days of alarms and
 *  says how many tickets a day it would have produced, and why the rest were
 *  left out.
 */
export function PolicyEditor({ integrationId, effective, onChange }: {
  integrationId: string;
  effective: IntegrationConfig;
  /** Reports only the clauses that MOVED. A save must stay sparse. */
  onChange: (edits: Partial<IntegrationPolicy>) => void;
}) {
  const [edits, setEdits] = useState<Partial<IntegrationPolicy>>({});
  const [days, setDays] = useState<number>(7);
  const policy = { ...effective.policy, ...edits };

  const preview = useMutation<PolicyPreview>({
    mutationFn: () => api.previewPolicy(integrationId, edits, days),
  });

  function set<K extends keyof IntegrationPolicy>(
    key: K, value: IntegrationPolicy[K],
  ) {
    // Dropped from the edit set when it returns to the stored value, so a
    // round trip through the form does not save a no-op as a change.
    const next = { ...edits };
    if (JSON.stringify(effective.policy[key]) === JSON.stringify(value)) {
      delete next[key];
    } else {
      next[key] = value;
    }
    setEdits(next);
    onChange(next);
  }

  /** One facet cell toggled. An empty result is refused by the server and
   *  would silently disable ticketing, so it is refused here too. */
  function toggleIn<K extends 'categories' | 'response_classes'>(
    key: K, value: string, all: readonly string[],
  ) {
    const current = policy[key];
    const next = current.includes(value)
      ? current.filter((v) => v !== value)
      : [...current, value];
    set(key, (next.length ? all.filter((a) => next.includes(a)) : [...all]) as never);
  }

  const dirty = Object.keys(edits).length > 0;
  const result = preview.data;

  const perDay = useMemo(() => {
    if (!result) return [];
    // Every day in the window, including the quiet ones. Dropping them would
    // turn a series of four busy days out of thirty into a solid bar chart
    // that reads as constant load.
    const out: { label: string; n: number }[] = [];
    for (let i = days - 1; i >= 0; i -= 1) {
      const d = new Date(Date.now() - i * 86_400_000).toISOString().slice(0, 10);
      out.push({ label: d.slice(5), n: result.per_day[d] ?? 0 });
    }
    return out;
  }, [result, days]);

  return (
    <div className="stack">
      <p className="muted">
        <Tip tip={oneLine(`Every clause reads a column the alarm already
                carries, so this costs nothing to evaluate. The escape hatch
                for anything it excludes is the Create ticket action on the
                alarm itself.`)}>
          A condition earns a ticket when it clears every clause below.
        </Tip>
      </p>

      <div className="form-grid">
      <div className="group" role="group" aria-labelledby="pol-urgency">
        <span id="pol-urgency">Urgency</span>
        <Seg value={policy.min_severity}
             label="Minimum severity"
             onChange={(v) => set('min_severity', v)}
             options={SEVERITIES.map((s) => ({
               key: s,
               label: result?.by_severity[s]
                 ? `${s} (${result.by_severity[s]})` : s,
             }))} />
        <small className="hint">
          At least this severe. A condition that escalates past the line later
          is still the same ticket, not a second one.
        </small>
      </div>

      <div className="group" role="group" aria-labelledby="pol-class">
        <span id="pol-class">Response class</span>
        <Facets values={CLASSES} on={policy.response_classes}
                label="Response class"
                onToggle={(v) => toggleIn('response_classes', v, CLASSES)}
                onAll={() => set('response_classes', [...CLASSES])} />
        <small className="hint">
          <b>alarm</b> demands action now and expects an acknowledgement.
          <b> alert</b> is informational and belongs to whoever schedules the
          work — ticketing it is how a service desk stops being read.
        </small>
      </div>

      <div className="group" role="group" aria-labelledby="pol-domains">
        <span id="pol-domains">Domains</span>
        <Facets values={CATEGORIES} on={policy.categories} label="Domains"
                counts={result?.by_category}
                onToggle={(v) => toggleIn('categories', v, CATEGORIES)}
                onAll={() => set('categories', [...CATEGORIES])} />
      </div>

      <label>
          <span>Hold for</span>
          <input type="number" min={0} max={86400} step={30}
                 value={policy.dwell_s}
                 onChange={(e) => set('dwell_s', Number(e.target.value))} />
          <small className="hint">
            Seconds a condition must have been open before its first ticket.
            Traps and equipment alarm points arrive pre-formed with no dwell of
            their own; this is theirs.
          </small>
      </label>

      <label className="switch">
        <input type="checkbox" checked={policy.exclude_symptoms}
               onChange={(e) => set('exclude_symptoms', e.target.checked)} />
        <span>Root causes only</span>
        <small className="hint">
          One switch failure is one incident with twenty suppressed symptoms
          on this console. Unticking this posts all twenty-one.
        </small>
      </label>

      <label className="switch">
        <input type="checkbox" checked={policy.exclude_shelved}
               onChange={(e) => set('exclude_shelved', e.target.checked)} />
        <span>Skip planned work</span>
        <small className="hint">
          Alarms shelved by a maintenance window. The window already has a
          change record.
        </small>
      </label>
      </div>

      {/* ------------------------------------------------------ rehearsal */}
      <section className="asset-panel">
        <h3>What this would have ticketed</h3>
        <div className="asset-panel-filters">
          <Seg value={String(days)} label="Range"
               onChange={(v) => setDays(Number(v))}
               options={RANGES.map((d) => ({ key: String(d), label: `${d}D` }))} />
          <button type="button" onClick={() => preview.mutate()}
                  disabled={preview.isPending}>
            {preview.isPending ? 'Replaying…' : 'Replay against real alarms'}
          </button>
          {dirty && <span className="muted">unsaved changes are included</span>}
        </div>

        {preview.error && (
          <div className="banner">
            {String((preview.error as Error).message)}
          </div>
        )}

        {!result && !preview.isPending && (
          <p className="muted">
            Not replayed yet. This reads alarms that were actually raised and
            creates nothing.
          </p>
        )}

        {result && (
          <>
            <p className="muted">
              <b>{result.tickets.toLocaleString()}</b> tickets from{' '}
              {result.matched.toLocaleString()} matching conditions, out of{' '}
              {result.considered.toLocaleString()} alarms in {days} days —
              about <b>{(result.tickets / days).toFixed(1)}</b> a day.
              {result.matched !== result.tickets && (
                <Tip tip={oneLine(`The same condition raised more than once is
                        one ticket. Counting rows would make every policy look
                        more expensive than it is.`)}>
                  {' '}Repeats are folded.
                </Tip>
              )}
            </p>
            <VColumns rows={perDay} caption="Tickets" />

            {result.excluded.length > 0 && (
              <div className="excluded">
                <h4>Left out</h4>
                <ul>
                  {result.excluded.slice(0, 6).map(([reason, n]) => (
                    <li key={reason}>
                      <span className="n">{n.toLocaleString()}</span> {reason}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}


/** A multi-select facet strip, in the shape the rulebook mandates and the
 *  alarm panel already draws: the segmented control, an ALL cell first, the
 *  count inside each cell in faint ink, cells combining as OR.
 *
 *  It borrows `.seg.facet-seg` from `estate.css` VERBATIM rather than styling
 *  itself, because a second set of values for one control is how two strips
 *  end up looking almost the same. It is not the alarm panel's `Facets`
 *  component because that one drops cells whose count is zero - correct there,
 *  where the counts are the whole population and an empty facet is noise, and
 *  wrong here, where a domain with no alarms this week must still be
 *  switchable off for next week.
 */
function Facets({ values, on, counts, label, onToggle, onAll }: {
  values: readonly string[];
  on: string[];
  counts?: Record<string, number>;
  label: string;
  onToggle: (value: string) => void;
  onAll: () => void;
}) {
  const all = on.length === values.length;
  return (
    <div className="seg facet-seg" role="group" aria-label={`${label} filter`}>
      <button type="button" className={all ? 'active' : ''} aria-pressed={all}
              onClick={onAll}>
        All
      </button>
      {values.map((v) => {
        const pressed = on.includes(v);
        return (
          <button key={v} type="button" className={pressed ? 'active' : ''}
                  aria-pressed={pressed} onClick={() => onToggle(v)}>
            {v.replace('_', ' ')}
            {counts?.[v] != null && <b>{counts[v]}</b>}
          </button>
        );
      })}
    </div>
  );
}
