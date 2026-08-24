/** The two modals behind the alert strip.
 *
 *  `AlertLegend` says what each counter counts. `AlertDrilldown` lists the
 *  rooms behind one counter, so the number on the strip has somewhere to go -
 *  a headline that cannot be opened is a headline an operator has to take on
 *  trust.
 *
 *  Both are read-only. Acknowledging and clearing live on the alarm list,
 *  which is one click away, because a modal that mutates state is a modal that
 *  needs a confirmation flow, an error state and an undo.
 */
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type AlertCategory } from '../../api/client';
import { Modal } from '../../components/estate';
import { CategoryGlyph, type GlyphKind } from '../../components/CategoryGlyph';

/** Mirrors `core/alarm_categories.py`. The wording is the contract: if a rule
 *  moves between buckets there, this text has to move with it. */
const DEFINITIONS: {
  key: AlertCategory; glyph: GlyphKind; title: string; what: string; examples: string;
}[] = [
  {
    key: 'thermal', glyph: 'thermal', title: 'Thermal',
    what: 'A temperature has crossed a threshold - rack intake, CPU, or room ambient.',
    examples: 'inlet_temp_high, cpu_temp_critical, ambient_temp_high',
  },
  {
    key: 'connectivity', glyph: 'connectivity', title: 'Connectivity',
    what: 'We cannot reach the device, or the collector that polls it has gone quiet. '
      + 'The equipment may be fine; what has failed is our view of it.',
    examples: 'endpoint_unreachable, collector_stale, assignment_stale',
  },
  {
    key: 'datapoint', glyph: 'datapoint', title: 'Datapoint',
    what: 'The device answers, but a value is not arriving - or the pipeline behind it '
      + 'has stalled. Distinct from connectivity because the fix is different.',
    examples: 'telemetry_stale, ingest_lag_high, ingest_stalled',
  },
  {
    key: 'anomaly', glyph: 'anomaly', title: 'Analytics',
    what: 'Raised by analysis rather than by a threshold. Nothing raises these yet: '
      + 'no anomaly engine is running, so this counter reads zero by construction '
      + 'rather than because the estate is quiet.',
    examples: 'anomaly_*, forecast_*',
  },
  {
    key: 'other', glyph: 'alarms', title: 'Other',
    what: 'Everything else - a plain threshold crossing that fits no bucket above.',
    examples: 'anything unmatched',
  },
];

export function AlertLegend({ onClose }: { onClose: () => void }) {
  return (
    <Modal title="Alert status" onClose={onClose}
           blurb={'Categories are mutually exclusive: every open alarm lands in exactly '
             + 'one, which is what lets the counters be read side by side. Counts are '
             + 'ROOTS only - one failed uplink is one alert, not one per device behind it.'}>
      <div className="legend-grid">
        {DEFINITIONS.map((d) => (
          <div className="legend-col" key={d.key}>
            <h4>
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                <CategoryGlyph kind={d.glyph} /> {d.title}
              </span>
            </h4>
            <p>{d.what}</p>
            <div className="legend-item">
              <span className={`legend-swatch ${d.key}`} />
              <span className="mono" style={{ fontSize: 11 }}>{d.examples}</span>
            </div>
          </div>
        ))}
      </div>
      <p className="muted small" style={{ marginTop: 18 }}>
        Acknowledged alarms are still counted. An acknowledged fault is a known
        fault, not a fixed one, and dropping it here is how it becomes a
        forgotten one. <Link to="/alarms">Open the alarm list →</Link>
      </p>
    </Modal>
  );
}

export function AlertDrilldown({ category, onClose }: {
  category: AlertCategory; onClose: () => void;
}) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['estate-alerts', category],
    queryFn: () => api.estateAlerts(category),
  });
  const def = DEFINITIONS.find((d) => d.key === category);

  return (
    <Modal title={`${def?.title ?? category} alerts`} count={data?.total}
           blurb={def?.what} onClose={onClose}>
      {error && <div className="banner">Could not load the drill-down.</div>}
      {isLoading && <p className="muted">Loading…</p>}

      {data && data.rows.length === 0 && (
        <p className="muted">
          No room has an open {def?.title.toLowerCase() ?? category} alert.
        </p>
      )}

      {data && data.rows.length > 0 && (
        <div className="estate-scroll">
          <table className="estate-table">
            <thead>
              <tr>
                <th>Room</th><th>Site</th>
                <th className="mid">Floor</th>
                <th className="num">Alerts</th>
                <th className="num">Devices</th>
                <th className="num">Critical</th>
                <th className="num">Major</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.rows.map((r) => (
                <tr key={r.room_id} className={r.critical ? 'lead-critical' : 'lead-warn'}>
                  <td><span className="name-cell"><span className="n">{r.room_name}</span></span></td>
                  <td className="muted">{r.site_code}</td>
                  <td className="mid muted">{r.floor ?? '—'}</td>
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

      {!!data?.unlocated && (
        <p className="muted small" style={{ marginTop: 14 }}>
          {data.unlocated} of these belong to no room - platform alarms hang off
          the pipeline rather than off a device on a floor, so they are counted
          in the total above but have no row.{' '}
          <Link to="/platform">Platform health →</Link>
        </p>
      )}
    </Modal>
  );
}
