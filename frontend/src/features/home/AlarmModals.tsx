/** The legend behind the alarm strip.
 *
 *  `AlarmLegend` says what each counter counts - and what the console leaves
 *  out, which matters more here than it looks: this page shows alarms only,
 *  and an operator who does not know that will read "0 cooling" as "no cooling
 *  problems" rather than "no cooling problems anybody must act on tonight".
 *
 *  The rooms behind a counter live in `AlarmPanel`, which is a work surface
 *  rather than an explanation and earns a full window of its own.
 *
 *  Neither writes the taxonomy down. Labels, owners, definitions and examples
 *  all come from `/estate/alert-categories`, which is generated from the
 *  classifier that fills the counters: a legend maintained beside the
 *  classifier drifts from it, and the first symptom is an operator routing work
 *  by a definition that stopped being true.
 *
 *  Both are read-only. Acknowledging and clearing live on the alarm list,
 *  which is one click away, because a modal that mutates state is a modal that
 *  needs a confirmation flow, an error state and an undo.
 */
import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type AlarmTaxonomy } from '../../api/client';
import { Modal } from '../../components/estate';
import { CategoryGlyph } from '../../components/CategoryGlyph';
import { metaFor } from '../../components/alertMeta';

function useTaxonomy() {
  return useQuery<AlarmTaxonomy>({
    queryKey: ['alarm-taxonomy'],
    queryFn: api.alarmTaxonomy,
    staleTime: Infinity,
  });
}

/** One row of the catalogue: what it is, how loud, and where it comes from. */
function ConditionRow({ c, categoryLabel }: {
  c: AlarmTaxonomy['conditions'][number]; categoryLabel: string;
}) {
  const cls = c.response_class;
  return (
    <tr className={c.enabled ? undefined : 'is-off'}>
      <td>
        <span className="name-cell"><span className="n">{c.label}</span></span>
        <div className="mono muted" style={{ fontSize: 10 }}>{c.key}</div>
      </td>
      <td className="muted">{categoryLabel}</td>
      <td>
        {cls
          ? <span className={`facet ${cls === 'alarm' ? 'alarms' : ''}`}>
              {cls === 'alarm' ? 'Alarm' : 'Alert'}
            </span>
          // Not "unknown": the severity arrives with the condition, and the
          // class follows it. Saying so is the honest answer to "will this
          // ring", and pretending otherwise would be a promise we cannot keep.
          : <span className="muted small">follows severity</span>}
      </td>
      <td className="muted small">{c.severity ?? 'varies'}</td>
      <td className="muted small">
        {c.detail}
        {!c.enabled && <span className="dash"> · disabled</span>}
      </td>
    </tr>
  );
}

/** Every condition, grouped the way the counters are. */
function Catalogue({ data }: { data: AlarmTaxonomy }) {
  const [only, setOnly] = useState<'all' | 'alarm' | 'alert'>('all');

  const labels = useMemo(() => Object.fromEntries(
    data.categories.map((c) => [c.key, c.label])), [data]);

  const rows = data.conditions.filter(
    (c) => only === 'all' || c.response_class === only);

  const s = data.summary;
  return (
    <>
      <p className="muted small" style={{ margin: '0 0 10px' }}>
        {s.total} conditions this platform knows how to raise: {s.alarm} always
        ring, {s.alert} never do, and {s.varies} follow the severity they arrive
        with. {s.planned > 0 && `${s.planned} are reserved names with no detector behind them yet. `}
        {s.disabled > 0 && `${s.disabled} rule${s.disabled === 1 ? ' is' : 's are'} switched off.`}
      </p>

      <div className="facet-row">
        <span className="facet-label">Show</span>
        {(['all', 'alarm', 'alert'] as const).map((k) => (
          <button key={k} className={`facet ${only === k ? 'on' : ''}`}
                  aria-pressed={only === k}
                  onClick={() => setOnly(k)}>
            {k === 'all' ? 'Everything' : k === 'alarm' ? 'Alarms' : 'Alerts'}
            <b>{k === 'all' ? s.total : k === 'alarm' ? s.alarm : s.alert}</b>
          </button>
        ))}
      </div>

      <div className="estate-scroll" style={{ maxHeight: '46vh' }}>
        <table className="estate-table catalogue">
          <thead>
            <tr>
              <th>Condition</th>
              <th>Category</th>
              <th>Class</th>
              <th>Severity</th>
              <th>Raised by</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <ConditionRow key={`${c.origin}-${c.key}`} c={c}
                            categoryLabel={c.category
                              ? (labels[c.category] ?? c.category)
                              : 'by the equipment'} />
            ))}
          </tbody>
        </table>
      </div>

      <div className="legend-grid" style={{ marginTop: 18 }}>
        {data.origins.map((o) => (
          <div className="legend-col" key={o.key}>
            <h4>{o.label}</h4>
            <p>{o.text}</p>
          </div>
        ))}
      </div>
    </>
  );
}

export function AlarmLegend({ onClose }: { onClose: () => void }) {
  const { data, isLoading, error } = useTaxonomy();
  const [tab, setTab] = useState<'axes' | 'catalogue'>('axes');

  return (
    <Modal title="Alarm status" onClose={onClose}
           blurb={'Everything on this page is an alarm: a condition that requires a '
             + 'response now. One axis - what kind of thing is wrong, and therefore '
             + 'who owns the first five minutes. Categories are mutually exclusive, '
             + 'so the counters sum to the total. Counts are ROOTS only: one failed '
             + 'uplink is one alarm, not one per device behind it.'}>
      {error && <div className="banner">Could not load the alarm taxonomy.</div>}
      {isLoading && <p className="muted">Loading…</p>}

      {data && (
        <div className="tabs" style={{ margin: '0 0 16px' }}>
          <button className={`tab ${tab === 'axes' ? 'active' : ''}`}
                  onClick={() => setTab('axes')}>WHAT THE COUNTERS MEAN</button>
          <button className={`tab ${tab === 'catalogue' ? 'active' : ''}`}
                  onClick={() => setTab('catalogue')}>
            EVERY CONDITION · {data.summary.total}
          </button>
        </div>
      )}

      {data && tab === 'catalogue' && <Catalogue data={data} />}

      {data && tab === 'axes' && (
        <>
          <div className="legend-grid">
            {data.categories.map((c) => {
              const meta = metaFor(c.key);
              return (
                <div className="legend-col" key={c.key}>
                  <h4>
                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                      <span className={`cat-${meta.tone}`}
                            style={{ display: 'inline-flex' }}>
                        <CategoryGlyph kind={meta.glyph} />
                      </span>
                      {c.label}
                      {/* Who acts, printed with the definition. A category an
                          operator cannot route is a category they will argue
                          about instead of using. */}
                      <span className="muted small" style={{ letterSpacing: 0 }}>
                        · {c.owner}
                      </span>
                    </span>
                  </h4>
                  <p>{c.description}</p>
                  <div className="legend-item">
                    <span className={`legend-swatch ${meta.tone}`} />
                    <span className="mono" style={{ fontSize: 11 }}>
                      {c.examples.length
                        ? c.examples.join(', ')
                        : 'no named condition - whatever the classifier does not match'}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>

          <h4 style={{ margin: '22px 0 6px', fontSize: 12, letterSpacing: '.06em',
                       textTransform: 'uppercase' }}>
            What this console leaves out
          </h4>
          <p className="muted small" style={{ margin: '0 0 10px' }}>
            Operations standards separate the two by REQUIRED RESPONSE, not by
            how bad the number looks. This page counts the first kind only.
            The second is classified and stored, and reachable from the{' '}
            <Link to="/alarms?response_class=alert">alarm list</Link> — worth
            knowing, because “0 cooling” here means nothing to act on tonight,
            not nothing at all.
          </p>
          <div className="legend-grid">
            {data.response_classes.map((c) => (
              <div className="legend-col" key={c.key}>
                <h4>{c.label}</h4>
                <p>{c.description}</p>
              </div>
            ))}
          </div>

          <h4 style={{ margin: '22px 0 6px', fontSize: 12, letterSpacing: '.06em',
                       textTransform: 'uppercase' }}>
            How it was found
          </h4>
          <p className="muted small" style={{ margin: '0 0 10px' }}>
            An attribute, not a category. Filtering on “derived” shows what
            analysis noticed across every domain - so improving a detector never
            moves an alarm from one counter to another.
          </p>
          <div className="legend-grid">
            {data.detections.map((d) => (
              <div className="legend-col" key={d.key}>
                <h4>{d.label}</h4>
                <p>{d.description}</p>
              </div>
            ))}
          </div>
        </>
      )}

      <p className="muted small" style={{ marginTop: 18 }}>
        Acknowledged alarms are still counted. An acknowledged fault is a known
        fault, not a fixed one, and dropping it here is how it becomes a
        forgotten one. <Link to="/alarms">Open the alarm list →</Link>
      </p>
    </Modal>
  );
}
