import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type TopologyNode, type Trace } from '../../api/client';
import { terminationLabel } from './TraceTable';

/** What is selected, and what feeds it.
 *
 *  A drawer rather than a modal. The diagram is there to be compared against,
 *  and a modal over it hides the one thing the reader is holding in their head
 *  - which is also why the full hop table is a view tab and not a popup.
 *
 *  The chain here is names and sides only. The outlet numbers and ratings are
 *  in the table, because they are what gets pasted into a ticket and this is
 *  what gets glanced at.
 */
export function Drawer({ node, layer, onFullTrace, onClose, onSimulate, simulating }: {
  node: TopologyNode;
  layer: string;
  onFullTrace: () => void;
  onClose: () => void;
  onSimulate: (deviceId: string | null) => void;
  simulating: boolean;
}) {
  // A rolled-up node is a synthetic id standing for a rack's worth of leaf
  // equipment. It has no device page and no trace of its own, and asking the
  // server for one would be a 400 on every click.
  const rolled = node.rolled_up > 0;

  const trace = useQuery<Trace>({
    queryKey: ['trace', node.id, layer],
    queryFn: () => api.trace(node.id, layer),
    enabled: !rolled,
    retry: false,
  });

  return (
    <aside className="conn-drawer" aria-label="Selected device">
      <header>
        <div>
          <h3>{node.name}</h3>
          <p className="k">
            {rolled
              ? `${node.rolled_up} × ${node.device_type.replace(/_/g, ' ')}`
              : node.device_type.replace(/_/g, ' ')}
          </p>
        </div>
        <button type="button" className="asset-max" aria-label="Clear selection"
                onClick={onClose}>✕</button>
      </header>

      {/* The facts scroll with the chains rather than holding the top: with a
          sheet up the drawer is only ~200px tall, and a fixed header plus a
          fixed facts list left the scroller nothing - which under
          `overflow: hidden` clips the chain rather than scrolling it. */}
      <div className="conn-drawer-scroll">
        <dl className="conn-facts">
          <dt>Status</dt>
          <dd>
            {node.status.toLowerCase()}
            {rolled && node.offline_count > 0
              && ` · ${node.offline_count} offline`}
          </dd>
          {node.location.rack_name && (<><dt>Rack</dt><dd>{node.location.rack_name}</dd></>)}
          {node.location.room_name && (<><dt>Room</dt><dd>{node.location.room_name}</dd></>)}
          {node.metrics.power_w != null && (
            <><dt>Draw</dt><dd>{Math.round(node.metrics.power_w)} W</dd></>)}
          {node.metrics.inlet_temp_c != null && (
            <>
              <dt>{rolled ? 'Worst inlet' : 'Inlet'}</dt>
              <dd>{node.metrics.inlet_temp_c.toFixed(1)} °C</dd>
            </>)}
          {node.depth > 0 && (
            <><dt>Scope</dt><dd>{node.depth} hop{node.depth > 1 ? 's' : ''} outside</dd></>)}
        </dl>

        {rolled ? (
          <p className="muted">
            {node.rolled_up} devices collapsed into one box so the room is
            readable. Open the rack to see them individually.
            {node.location.rack_id && (
              <> <Link to={`/racks/${node.location.rack_id}`}>Rack elevation →</Link></>
            )}
          </p>
        ) : (
          <>
            <h4>Fed by</h4>
            {trace.isLoading && <div className="asset-skeleton" style={{ height: 60 }} />}
            {trace.isError && (
              <p className="muted">No {layer} chain is recorded for this device.</p>
            )}
            {trace.data?.is_source && (
              <p className="muted">
                Nothing feeds it on this layer — it is a source here.
              </p>
            )}
            {trace.data && !trace.data.is_source && trace.data.paths.map((p, i) => {
              const cord = p.hops[p.hops.length - 1];
              const source = p.hops[0];
              return (
                <div className="conn-chain" key={`${p.side}-${i}`}>
                  <div className="conn-chain-head">
                    <span className={`conn-side side-${(p.side || '').toLowerCase()}`}>
                      {p.side || '—'}
                    </span>
                    <span className="k">
                      {p.hops.length} hops
                      {p.verdict !== 'complete' && (
                        <span className="warn"> · {p.verdict}</span>
                      )}
                    </span>
                  </div>
                  <ol>
                    {p.hops.map((h) => (
                      <li key={h.connection_id}>
                        <Link to={`/devices/${h.up.id}`}>{h.up.name}</Link>
                        {h.alternates.length > 0 && (
                          <span className="k"> or {h.alternates.length} other</span>
                        )}
                      </li>
                    ))}
                    <li className="is-self">{node.name}</li>
                  </ol>
                  {/* Plenty of gear is cabled without port-level detail - an RPP
                      into a PDU is recorded as connected and no further - and
                      "into —" is noise rather than a fact. */}
                  <p className="k">
                    from {source.up.name}
                    {cord.down_termination.type !== 'none'
                      && <> · into {terminationLabel(cord.down_termination)}</>}
                  </p>
                </div>
              );
            })}
            {trace.data?.asymmetric && (
              <p className="warn">
                The two sides reach a source at different depths.
              </p>
            )}
          </>
        )}
      </div>

      {/* On the drawer's floor rather than at the end of the chains: a
          dual-fed device prints two chains of six hops, and these three are
          the whole point of having selected it. */}
      {!rolled && (
        <div className="conn-drawer-actions">
          {/* The question asked before every maintenance window, and the
              server has been able to answer it since the topology service
              landed. Nothing had ever asked. */}
          <button type="button" className={simulating ? 'is-on' : undefined}
                  onClick={() => onSimulate(simulating ? null : node.id)}>
            {simulating ? 'Stop simulating' : 'Simulate removal'}
          </button>
          <button type="button" onClick={onFullTrace}>Full trace</button>
          <Link to={`/devices/${node.id}`}>Open device →</Link>
        </div>
      )}
    </aside>
  );
}
