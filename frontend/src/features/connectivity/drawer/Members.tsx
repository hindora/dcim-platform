import { useQuery } from '@tanstack/react-query';
import { useCallback, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { api, type ElevationDevice, type RackElevation, type TopologyNode }
  from '../../../api/client';
import { usePaged } from '../../../components/Pagination';
import { downloadCsv, stampedName } from '../../../lib/csv';
import { formatValue, statusClass } from '../../../lib/format';
import { Loading, SEVERITY_RANK, useExport, type DrawerCtx } from './shared';

interface Member extends ElevationDevice { u?: number | null }

/** The devices a rack box stands for, worst first.
 *
 *  Read off the rack elevation rather than a device query per member: one call,
 *  and it already carries each device's status, draw and inlet. Offline sorts
 *  above everything, because a box with a green border and one dead server in
 *  it is exactly what a roll-up is at risk of hiding.
 */
export function Members({ node, ctx }: { node: TopologyNode; ctx: DrawerCtx }) {
  const rackId = node.location.rack_id;
  const q = useQuery<RackElevation>({
    queryKey: ['rack-elevation', rackId],
    queryFn: () => api.rackElevation(rackId!),
    enabled: Boolean(rackId),
  });

  const members = useMemo<Member[]>(() => {
    if (!q.data) return [];
    const want = new Set(node.member_ids);
    const found: Member[] = [
      ...q.data.positions.flatMap((s) => (s.device ? [{ ...s.device, u: s.u_start }] : [])),
      ...q.data.zero_u_devices.map((d) => ({ ...d, u: null })),
    ].filter((d) => want.has(d.id));
    const bad = (d: Member) => (d.status === 'OFFLINE' ? -1 : SEVERITY_RANK[d.max_severity] ?? 5);
    return found.sort((a, b) => bad(a) - bad(b) || (b.u ?? 0) - (a.u ?? 0));
  }, [q.data, node.member_ids]);

  const exporter = useCallback(() => downloadCsv(
    stampedName(`rack-${node.location.rack_name ?? node.name}`),
    ['Device', 'Type', 'U', 'Status', 'Severity', 'Draw W', 'Inlet C'],
    members.map((m) => [m.name, m.device_type, m.u ?? '0U', m.status, m.max_severity,
                        m.power_w ?? '', m.inlet_temp_c ?? '']),
  ), [members, node.location.rack_name, node.name]);
  useExport(ctx, members.length ? exporter : null);

  const { rows, foot } = usePaged(members, { noun: 'devices' });

  if (!rackId) return <p className="muted">Not in a rack, so there is no member list.</p>;
  if (q.isLoading) return <Loading h={120} />;

  return (
    <section>
      <h4>Members</h4>
      <p className="k">
        {node.rolled_up} × {node.device_type.replace(/_/g, ' ')} in one box so the
        room is readable. Double-click the box to draw them here, or switch
        grouping to "Every device" to open every rack at once.
        {' '}<Link to={`/racks/${rackId}`}>Rack elevation →</Link>
      </p>
      <ul className="cd-list">
        {rows.map((m) => (
          <li key={m.id}>
            <div className="cd-port-head">
              <span>
                <span className={`chip ${statusClass(m.status === 'OFFLINE'
                  ? 'OFFLINE' : m.max_severity)}`}>
                  <span className="dot" aria-hidden="true" />
                </span>
                <Link to={`/devices/${m.id}`}>{m.name}</Link>
              </span>
              <span className="k">{m.u != null ? `U${m.u}` : '0U'}</span>
            </div>
            <div className="cd-sub k">
              {[m.status.toLowerCase(),
                m.power_w != null && formatValue('W', m.power_w),
                m.inlet_temp_c != null && `${m.inlet_temp_c.toFixed(1)} °C in`]
                .filter(Boolean).join(' · ')}
            </div>
          </li>
        ))}
      </ul>
      {foot}
    </section>
  );
}
