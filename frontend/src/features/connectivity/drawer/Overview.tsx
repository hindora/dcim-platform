import type { DeviceDetail, DeviceState, TopologyNode } from '../../../api/client';
import { Meter } from '../../../components/Meter';
import { formatMetric, humanise, metricLabel, relativeTime } from '../../../lib/format';
import { METRICS, type MetricKey } from '../../../lib/metrics.gen';
import { DeviceRef, Loading, type DrawerCtx } from './shared';

/** Which metric groups answer "is it OK" on each layer, most telling first.
 *  Everything else the device reports still shows, under the fold. */
const LAYER_GROUPS: Record<string, string[]> = {
  power: ['power'],
  cooling: ['cooling', 'thermal', 'environment'],
  network: ['interfaces', 'system'],
  management: ['system', 'interfaces'],
  fieldbus: ['system'],
};

/** Is it OK, and how close to its limit.
 *
 *  The one meter is the number an electrical engineer asks for first - draw
 *  against the chassis nameplate, or a PDU's own load share - and it is only
 *  drawn when both ends are known. A draw with no rating is printed as a
 *  reading, not as a bar against nothing.
 */
export function Overview({ node, layer, detail, state, ctx }: {
  node: TopologyNode;
  layer: string;
  detail: DeviceDetail | undefined;
  state: DeviceState | undefined;
  ctx: DrawerCtx;
}) {
  if (!detail || !state) return <Loading h={160} />;

  const reading = (k: string) => {
    const v = state.metrics[k]?.v;
    return typeof v === 'number' ? v : null;
  };
  const draw = reading('power_draw');
  const loadPct = reading('load_pct');

  const primary = LAYER_GROUPS[layer] ?? [];
  const rank = (k: string) => {
    const g = METRICS[k as MetricKey]?.group ?? '';
    const i = primary.indexOf(g);
    return i < 0 ? primary.length : i;
  };
  const metrics = Object.entries(state.metrics)
    .filter(([, m]) => m.v !== null && m.v !== undefined)
    .sort(([a], [b]) => rank(a) - rank(b) || metricLabel(a).localeCompare(metricLabel(b)));
  const lead = metrics.filter(([k]) => rank(k) < primary.length);
  const rest = metrics.filter(([k]) => rank(k) >= primary.length);

  const loc = detail.location;
  const stale = (k: string) => state.metrics[k]?.q !== 'good';

  return (
    <>
      {draw !== null && detail.rated_power_w ? (
        <Meter label="Power draw" note="against the chassis nameplate" used={draw} capacity={detail.rated_power_w}
               unit="W" />
      ) : loadPct !== null ? (
        <Meter label="Load" used={loadPct} capacity={100} unit="%" />
      ) : null}

      {metrics.length > 0 && (
        <section>
          <h4>Live readings</h4>
          {[lead, rest].filter((rows) => rows.length).map((rows, i) => (
            <dl key={i} className={`conn-facts${i ? ' cd-fold' : ''}`}>
              {rows.map(([k, m]) => (
                <div key={k} className="cd-pair">
                  <dt>{metricLabel(k)}</dt>
                  <dd className={stale(k) ? 'muted' : undefined}>
                    {formatMetric(k, m.v)}
                    {stale(k) && <span className="k"> · {m.q}</span>}
                  </dd>
                </div>
              ))}
            </dl>
          ))}
          <p className="k">as of {relativeTime(state.last_seen)}</p>
        </section>
      )}

      {detail.psus.length > 0 && (
        <section>
          <h4>Power supplies</h4>
          <ul className="cd-list">
            {detail.psus.map((p) => (
              <li key={p.number}>
                <span className="cd-mono">PSU{p.number}</span>{' '}
                <span className="k">
                  {[p.connector, p.rated_watts != null && `${p.rated_watts} W`]
                    .filter(Boolean).join(' · ')}
                </span>
                <div className="cd-sub">
                  {p.feed_device_id && p.feed_device ? (
                    <>← <DeviceRef id={p.feed_device_id} name={p.feed_device} ctx={ctx} />
                      {p.feed_outlet != null && <> outlet {p.feed_outlet}</>}</>
                  ) : <span className="warn">fitted, not corded</span>}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section>
        <h4>Asset</h4>
        <dl className="conn-facts">
          {detail.model && (<><dt>Model</dt><dd>{detail.model}</dd></>)}
          {loc.rack_name && (
            <><dt>Position</dt>
              <dd>{loc.rack_name}{loc.u_start != null
                && ` · U${loc.u_start}${detail.u_height > 1
                  ? `–${loc.u_start + detail.u_height - 1}` : ''}`}</dd></>)}
          {loc.room_name && (<><dt>Room</dt><dd>{loc.room_name}</dd></>)}
          {detail.mgmt_ip && (<><dt>Mgmt IP</dt><dd className="cd-mono">{detail.mgmt_ip}</dd></>)}
          {detail.lifecycle && (<><dt>Lifecycle</dt><dd>{humanise(detail.lifecycle)}</dd></>)}
          <dt>Admin</dt><dd>{detail.admin_state}</dd>
          {detail.serial_number && (<><dt>Serial</dt><dd className="cd-mono">{detail.serial_number}</dd></>)}
          {node.depth > 0 && (
            <><dt>Scope</dt><dd>{node.depth} hop{node.depth > 1 ? 's' : ''} outside</dd></>)}
        </dl>
      </section>
    </>
  );
}
