/** The two modals behind the alert strip.
 *
 *  `AlertLegend` says what each counter counts. `AlertDrilldown` lists the
 *  rooms behind one counter, so the number on the strip has somewhere to go -
 *  a headline that cannot be opened is a headline an operator has to take on
 *  trust.
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
import { useQueries, useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  api,
  type AlertCategory,
  type AlertDetection,
  type AlertDrill,
  type AlertDrillRow,
  type AlertTaxonomy,
} from '../../api/client';
import { Modal } from '../../components/estate';
import { CategoryGlyph } from '../../components/CategoryGlyph';
import { metaFor } from '../../components/alertMeta';

const SEVERITIES = ['critical', 'major', 'minor', 'warning'] as const;

function useTaxonomy() {
  return useQuery<AlertTaxonomy>({
    queryKey: ['alert-taxonomy'],
    queryFn: api.alertTaxonomy,
    staleTime: Infinity,
  });
}

export function AlertLegend({ onClose }: { onClose: () => void }) {
  const { data, isLoading, error } = useTaxonomy();

  return (
    <Modal title="Alert status" onClose={onClose}
           blurb={'One axis: what kind of thing is wrong, and therefore who owns the '
             + 'first five minutes. Categories are mutually exclusive, so the counters '
             + 'sum to the total. Counts are ROOTS only - one failed uplink is one '
             + 'alert, not one per device behind it.'}>
      {error && <div className="banner">Could not load the alert taxonomy.</div>}
      {isLoading && <p className="muted">Loading…</p>}

      {data && (
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

/** Facet chips: the same population the rows total, split two ways.
 *
 *  Folded from the rows rather than fetched, so a facet can never describe a
 *  different instant of the estate than the table under it. */
function Facets({ label, entries }: {
  label: string; entries: { key: string; label: string; n: number }[];
}) {
  const shown = entries.filter((e) => e.n > 0);
  if (!shown.length) return null;
  return (
    <div className="facet-row">
      <span className="facet-label">{label}</span>
      {shown.map((e) => (
        <span key={e.key} className={`facet ${e.key}`}>
          {e.label}<b>{e.n}</b>
        </span>
      ))}
    </div>
  );
}

export function AlertDrilldown({ categories, title, onClose }: {
  categories: AlertCategory[]; title: string; onClose: () => void;
}) {
  // One query per category, never a merged one. A grouped counter such as
  // "Cooling & Environment" is two categories; asking for each separately and
  // adding the results keeps every number the server's, and the categories are
  // mutually exclusive so nothing is counted twice.
  const results = useQueries({
    queries: categories.map((category) => ({
      queryKey: ['estate-alerts', category],
      queryFn: () => api.estateAlerts(category),
    })),
  });

  const { data: taxonomy } = useTaxonomy();
  const isLoading = results.some((r) => r.isLoading);
  const error = results.find((r) => r.error)?.error;
  const loaded = results
    .map((r, i) => (r.data ? { category: categories[i], drill: r.data } : null))
    .filter(Boolean) as { category: AlertCategory; drill: AlertDrill }[];

  const total = loaded.reduce((n, d) => n + d.drill.total, 0);
  const unlocated = loaded.reduce((n, d) => n + d.drill.unlocated, 0);

  // Rows stay per room AND per category. Merging them would need a rule for
  // `devices`, where the same device can carry alerts in two categories, and a
  // guessed rule there is a number nobody can reconcile against the alarm list.
  const rows: { category: AlertCategory; row: AlertDrillRow }[] = loaded
    .flatMap((d) => d.drill.rows.map((row) => ({ category: d.category, row })))
    .sort((a, b) => b.row.qty - a.row.qty);

  const severity = SEVERITIES.map((k) => ({
    key: k,
    label: k.toUpperCase(),
    n: loaded.reduce((n, d) => n + (d.drill.by_severity?.[k] ?? 0), 0),
  }));

  const detections = (taxonomy?.detections ?? []).map((d) => ({
    key: d.key as AlertDetection,
    label: d.label,
    n: loaded.reduce((n, x) => n + (x.drill.by_detection?.[d.key] ?? 0), 0),
  }));

  const definitions = (taxonomy?.categories ?? []).filter(
    (c) => categories.includes(c.key));
  const blurb = definitions.map((c) => c.description).join(' ');

  return (
    <Modal title={`${title} alerts`} count={loaded.length ? total : undefined}
           blurb={blurb || undefined} onClose={onClose}>
      {error && <div className="banner">Could not load the drill-down.</div>}
      {isLoading && <p className="muted">Loading…</p>}

      {!!loaded.length && (
        <>
          <Facets label="Severity" entries={severity} />
          <Facets label="Found by" entries={detections} />
        </>
      )}

      {!!loaded.length && rows.length === 0 && (
        <p className="muted">No room has an open {title.toLowerCase()} alert.</p>
      )}

      {rows.length > 0 && (
        <div className="estate-scroll">
          <table className="estate-table">
            <thead>
              <tr>
                <th>Room</th><th>Site</th>
                <th className="mid">Floor</th>
                {categories.length > 1 && <th>Category</th>}
                <th className="num">Alerts</th>
                <th className="num">Devices</th>
                <th className="num">Critical</th>
                <th className="num">Major</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map(({ category, row: r }) => (
                <tr key={`${category}-${r.room_id}`}
                    className={r.critical ? 'lead-critical' : 'lead-warn'}>
                  <td><span className="name-cell"><span className="n">{r.room_name}</span></span></td>
                  <td className="muted">{r.site_code}</td>
                  <td className="mid muted">{r.floor ?? '—'}</td>
                  {categories.length > 1 && (
                    <td className="muted">
                      <span className={`cat-${metaFor(category).tone}`}
                            style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                        <CategoryGlyph kind={metaFor(category).glyph} />
                      </span>{' '}
                      {taxonomy?.categories.find((c) => c.key === category)?.label ?? category}
                    </td>
                  )}
                  <td className="num">{r.qty}</td>
                  <td className="num">{r.devices}</td>
                  <td className="num">{r.critical || <span className="dash">—</span>}</td>
                  <td className="num">{r.major || <span className="dash">—</span>}</td>
                  <td className="num">
                    <Link className="row-btn" to={`/alarms?room=${r.room_id}`}
                          style={{ display: 'inline-block', lineHeight: '26px', textAlign: 'center' }}>
                      OPEN
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {unlocated > 0 && (
        <p className="muted small" style={{ marginTop: 14 }}>
          {unlocated} of these belong to no room - platform alarms hang off
          the pipeline rather than off a device on a floor, so they are counted
          in the total above, have no row, and are left out of the facets.{' '}
          <Link to="/platform">Platform health →</Link>
        </p>
      )}
    </Modal>
  );
}
