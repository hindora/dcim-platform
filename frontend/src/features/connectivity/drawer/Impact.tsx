import { useQuery } from '@tanstack/react-query';
import { useCallback, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { api, type Impact as ImpactOut, type ImpactNode, type MaintenanceWindow,
         type TopologyNode } from '../../../api/client';
import { usePaged } from '../../../components/Pagination';
import { downloadCsv, stampedName } from '../../../lib/csv';
import { DeviceRef, Loading, useExport, type DrawerCtx } from './shared';

/** What breaks if it is removed, and whether that is already planned.
 *
 *  The same answer "Simulate removal" paints on the canvas, as a list: the
 *  overlay shows where, this says who. The layer on screen comes first, but
 *  every layer is listed - pulling a PDU is a power event that is also a
 *  monitoring event, and a method statement has to name both.
 */
export function Impact({ node, layer, windows, ctx }: {
  node: TopologyNode;
  layer: string;
  windows: MaintenanceWindow[];
  ctx: DrawerCtx;
}) {
  const q = useQuery<ImpactOut>({
    queryKey: ['impact', node.id],
    queryFn: () => api.impact(node.id),
    retry: false,
  });

  const layers = useMemo(() => {
    const ls = q.data?.layers ?? [];
    const onScreen = (l: string) => l === layer
      || (layer === 'network' && l === 'production');
    return [...ls].sort((a, b) => Number(onScreen(b.layer)) - Number(onScreen(a.layer)));
  }, [q.data, layer]);

  const d = q.data;
  const exporter = useCallback(() => {
    if (!d) return;
    downloadCsv(
      stampedName(`impact-${d.device.name}`),
      ['Layer', 'Effect', 'Outcome', 'Device', 'Type', 'Rack', 'Room'],
      d.layers.flatMap((l) => [
        ...l.cut_off.map((n) => [l.layer, l.effect, 'cut off', n.name,
                                 n.device_type, n.rack_name, n.room_name]),
        ...l.degraded.map((n) => [l.layer, l.effect, 'degraded', n.name,
                                  n.device_type, n.rack_name, n.room_name]),
      ]),
    );
  }, [d]);
  useExport(ctx, d && (d.total_cut_off || d.total_degraded) ? exporter : null);

  return (
    <>
      {windows.length > 0 && (
        <section>
          <h4>Maintenance</h4>
          <ul className="cd-list">
            {windows.map((w) => (
              <li key={w.id}>
                <Link to={`/assets/maintenance/${w.id}`}>{w.title}</Link>
                <div className="k">
                  {w.status === 'active' ? <b className="warn">in progress</b> : 'scheduled'}
                  {' · '}
                  {w.status === 'active'
                    ? `ends ${new Date(w.ends_at).toLocaleString()}`
                    : `starts ${new Date(w.starts_at).toLocaleString()}`}
                  {w.change_ref && <> · {w.change_ref}</>}
                  {w.suppress && <> · alarms held</>}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section>
      <h4>If removed</h4>
      {q.isLoading && <Loading h={100} />}
      {q.isError && <p className="muted">Could not work out what depends on it.</p>}
      {d && (
        <div className="cd-impact-sum">
          <div data-tone={d.total_cut_off ? 'critical' : 'ok'}>
            <b>{d.total_cut_off}</b><span>cut off</span>
          </div>
          <div data-tone={d.total_degraded ? 'warn' : 'ok'}>
            <b>{d.total_degraded}</b><span>lose a side</span>
          </div>
        </div>
      )}
      {d && !d.total_cut_off && !d.total_degraded && (
        <p className="muted">Nothing depends on it on any layer.</p>
      )}
      {d && (
        <p className="k">
          Worked out from the recorded topology, not from live load: a
          surviving side is still served, not necessarily able to carry it.
        </p>
      )}
      </section>

      {layers.filter((l) => l.cut_off.length || l.degraded.length).map((l) => (
        <section key={l.layer} className="cd-impact-layer">
          <h5>{l.layer} <span className="k">· {l.effect}</span></h5>
          <ImpactList title="Cut off" tone="critical" items={l.cut_off} ctx={ctx} />
          <ImpactList title="Lose a side" tone="warn" items={l.degraded} ctx={ctx} />
        </section>
      ))}
    </>
  );
}

function ImpactList({ title, tone, items, ctx }: {
  title: string;
  tone: string;
  items: ImpactNode[];
  ctx: DrawerCtx;
}) {
  const { rows, foot } = usePaged(items, { noun: 'devices' });
  if (!items.length) return null;
  return (
    <>
      <div className="cd-impact-title" data-tone={tone}>{title} · {items.length}</div>
      <ul className="cd-list cd-compact">
        {rows.map((n) => (
          <li key={n.id}>
            <DeviceRef id={n.id} name={n.name} ctx={ctx} />
            <span className="k"> {[n.rack_name, n.room_name].filter(Boolean).join(' · ')}</span>
          </li>
        ))}
      </ul>
      {foot}
    </>
  );
}
