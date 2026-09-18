import { useQuery } from '@tanstack/react-query';
import { useCallback, useMemo, useState } from 'react';
import { api, type NetworkInterface, type TopologyNode } from '../../../api/client';
import { Seg } from '../../../components/estate';
import { usePaged } from '../../../components/Pagination';
import { downloadCsv, stampedName } from '../../../lib/csv';
import { formatSpeed } from '../../../lib/format';
import { DeviceRef, Loading, useExport, type DrawerCtx } from './shared';

/** Its ports, and what is on the far end of each.
 *
 *  Patched and disabled ports by default. A 48-port leaf is mostly spares,
 *  and a list that is mostly spares hides the six cables anyone came to read;
 *  they are one toggle away for whoever is looking for a free port.
 */
export function Ports({ node, ctx }: { node: TopologyNode; ctx: DrawerCtx }) {
  const [show, setShow] = useState<'used' | 'all'>('used');
  const q = useQuery<NetworkInterface[]>({
    queryKey: ['interfaces', node.id],
    queryFn: () => api.interfaces(node.id),
    retry: false,
  });

  const all = q.data;
  const used = useMemo(
    () => (all ?? []).filter((p) => p.peer_device || p.admin_state !== 'enabled'),
    [all]);
  const items = show === 'all' ? (all ?? []) : used;

  const exporter = useCallback(() => downloadCsv(
    stampedName(`ports-${node.name}`),
    ['Port', 'Role', 'Speed', 'IP', 'MAC', 'Admin', 'Peer device', 'Peer port', 'Layer'],
    (all ?? []).map((p) => [p.name, p.role, formatSpeed(p.speed_bps), p.ip ?? '',
                            p.mac ?? '', p.admin_state, p.peer_device ?? '',
                            p.peer_port ?? '', p.peer_layer ?? '']),
  ), [all, node.name]);
  useExport(ctx, all?.length ? exporter : null);

  const { rows, foot } = usePaged(items, { noun: 'ports' });

  if (q.isLoading) return <Loading h={120} />;
  if (q.isError || !all) return <p className="muted">No ports are recorded for it.</p>;

  return (
    <section>
      <h4>Ports</h4>
      <div className="cd-toolbar">
        <Seg label="Which ports" value={show} onChange={setShow}
             options={[{ key: 'used', label: `In use ${used.length}` },
                       { key: 'all', label: `All ${all.length}` }]} />
      </div>
      {items.length === 0 && <p className="muted">No port is patched.</p>}
      <ul className="cd-list cd-ports">
        {rows.map((p) => (
          <li key={p.id} className={p.admin_state !== 'enabled' ? 'is-off' : undefined}>
            <div className="cd-port-head">
              <span className="cd-mono">{p.name}</span>
              <span className="k">
                {[p.role === 'mgmt' ? 'mgmt' : null, formatSpeed(p.speed_bps),
                  p.admin_state !== 'enabled' ? p.admin_state : null]
                  .filter((x) => x && x !== '—').join(' · ')}
              </span>
            </div>
            <div className="cd-sub">
              {p.peer_device && p.peer_device_id ? (
                <>→ <DeviceRef id={p.peer_device_id} name={p.peer_device} ctx={ctx} />
                  {p.peer_port && <span className="cd-mono k"> {p.peer_port}</span>}</>
              ) : <span className="k">spare</span>}
            </div>
          </li>
        ))}
      </ul>
      {foot}
    </section>
  );
}
