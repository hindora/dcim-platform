import { useQuery } from '@tanstack/react-query';
import { useCallback } from 'react';
import { api, type PowerChain, type TopologyNode, type Trace } from '../../../api/client';
import { usePaged } from '../../../components/Pagination';
import { exportTraceCsv, PORT_LAYERS, terminationLabel } from '../trace';
import { DeviceRef, Loading, useExport, type DrawerCtx } from './shared';

/** Upstream to source, and what hangs off it downstream.
 *
 *  Every hop carries the port it leaves by and the port it lands on, because
 *  the outlet number and rating are what a technician needs in front of the
 *  PDU. On the power layer the redundancy verdict sits on top: it is the one
 *  line that says whether the two chains below are really two.
 */
export function Chain({ node, layer, ctx }: {
  node: TopologyNode;
  layer: string;
  ctx: DrawerCtx;
}) {
  const trace = useQuery<Trace>({
    queryKey: ['trace', node.id, layer],
    queryFn: () => api.trace(node.id, layer),
    retry: false,
  });
  const chain = useQuery<PowerChain>({
    queryKey: ['power-chain', node.id],
    queryFn: () => api.powerChain(node.id),
    enabled: layer === 'power',
    retry: false,
  });
  const showState = PORT_LAYERS.has(layer);

  const t = trace.data;
  const exporter = useCallback(
    () => { if (t) exportTraceCsv(t, node.name, layer); },
    [t, node.name, layer]);
  useExport(ctx, t?.paths.length ? exporter : null);

  const downstream = t?.downstream ?? [];
  const { rows: down, foot } = usePaged(downstream, { noun: 'devices' });

  return (
    <>
      {chain.data && (
        // Healthy only when every recorded path is live AND there is more than
        // one: a single-corded load is "fine" until the day it is not.
        <div className="cd-verdict" data-tone={chain.data.live_paths >= 2
          && chain.data.live_paths === chain.data.total_paths ? 'ok' : 'warn'}>
          <b>{chain.data.redundancy}</b>
          <span>{chain.data.reason}</span>
        </div>
      )}

      <h4>Fed by</h4>
      {trace.isLoading && <Loading />}
      {trace.isError && <p className="muted">No {layer} chain is recorded for this device.</p>}
      {t?.is_source && (
        <p className="muted">Nothing feeds it on this layer — it is a source here.</p>
      )}
      {t && !t.is_source && t.paths.map((p, i) => (
        <div className="conn-chain" key={`${p.side}-${i}`}>
          <div className="conn-chain-head">
            <span className={`conn-side side-${(p.side || '').toLowerCase()}`}>
              {p.side || '—'}
            </span>
            <span className="k">
              {p.hops.length} hops
              {p.verdict !== 'complete' && <span className="warn"> · {p.verdict}</span>}
            </span>
          </div>
          <ol>
            {p.hops.map((h) => {
              // Plenty of gear is cabled without port-level detail - an RPP
              // into a PDU is recorded as connected and no further - and
              // "— → —" is noise rather than a fact.
              const ported = h.up_termination.type !== 'none'
                || h.down_termination.type !== 'none';
              const linkDown = showState && h.oper_state === 'down';
              return (
                <li key={h.connection_id}>
                  <DeviceRef id={h.up.id} name={h.up.name} ctx={ctx} />
                  {h.alternates.length > 0 && (
                    // The fork lives on the hop where it happens rather than
                    // multiplying the chain out once per source.
                    <div className="k conn-alt">
                      or {h.alternates.map((a) => a.name).join(', ')}
                    </div>
                  )}
                  {(ported || linkDown) && (
                    <div className="conn-port">
                      {ported && <>{terminationLabel(h.up_termination)}
                        {' → '}{terminationLabel(h.down_termination)}</>}
                      {linkDown && <span className="warn"> · link down</span>}
                    </div>
                  )}
                </li>
              );
            })}
            <li className="is-self">{node.name}</li>
          </ol>
        </div>
      ))}
      {t?.asymmetric && (
        <p className="warn">The two sides reach a source at different depths.</p>
      )}
      {t?.truncated && <p className="warn">More cords than can be listed.</p>}

      {t && t.downstream_count > 0 && (
        <section>
          <h4>Feeds {t.downstream_count} device{t.downstream_count === 1 ? '' : 's'}</h4>
          <ul className="cd-list">
            {down.map((d) => (
              <li key={d.device.id}>
                {d.redundancy_side && (
                  <span className={`conn-side side-${d.redundancy_side.toLowerCase()}`}>
                    {d.redundancy_side}
                  </span>
                )}
                <DeviceRef id={d.device.id} name={d.device.name} ctx={ctx} />
                {d.termination.type !== 'none' && (
                  <div className="conn-port">{terminationLabel(d.termination)}</div>
                )}
              </li>
            ))}
          </ul>
          {t.downstream.length < t.downstream_count && (
            <p className="k">
              First {t.downstream.length} listed. Impact has the full set.
            </p>
          )}
          {foot}
        </section>
      )}
    </>
  );
}
