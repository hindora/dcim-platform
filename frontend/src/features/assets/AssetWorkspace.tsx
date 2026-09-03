import { NavLink, Outlet } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api, type AssetSummary } from '../../api/client';
import './assets.css';

/** The /assets workspace shell.
 *
 *  Everything the asset module renders lives under this route and inside this
 *  directory. No page outside /assets changes - see docs/22 §1 for the rule and
 *  what it costs. The rail is the module's own navigation; the app's main menu
 *  keeps its single `Assets` entry.
 *
 *  Sections rather than a flat list, because eleven items is a wall. The counts
 *  are the point: the rail doubles as a work queue, so a badge means somebody
 *  has something to do here. A zero renders as no badge at all - "(0)" is
 *  visual noise that reads as a number worth checking.
 */

interface NavItem {
  to: string;
  label: string;
  /** Undefined = this section has no queue. Null = not tracked yet. */
  count?: number | null;
  end?: boolean;
}

export function AssetWorkspace() {
  // Shared with Overview through the query cache, so the rail's badges cost
  // nothing extra: one fetch feeds both.
  const { data } = useQuery<AssetSummary>({
    queryKey: ['asset-summary'],
    queryFn: api.assetSummary,
    refetchInterval: 60_000,
  });

  const { data: active } = useQuery({
    queryKey: ['maintenance-windows', 'active'],
    queryFn: () => api.maintenanceWindows({ status: 'active' }),
    refetchInterval: 60_000,
  });

  // Running windows are a badge because they change how the whole console
  // reads: alarms are being held back somewhere, and an operator should be able
  // to see that from any page in the module.
  const windows = active?.items.length || undefined;
  // Expiring cover is a queue, not a status: somebody has to renew it, and the
  // badge is how they find out before it lapses rather than after.
  const expiring = data?.contracts
    ? (data.contracts.expiring + data.contracts.expired) || undefined
    : undefined;

  const sections: { title: string; items: NavItem[] }[] = [
    {
      title: 'Estate',
      items: [
        { to: '/assets', end: true, label: 'Overview' },
        { to: '/assets/inventory', label: 'Inventory' },
        { to: '/assets/estate', label: 'Placement' },
      ],
    },
    {
      title: 'Intake',
      items: [
        { to: '/assets/discovery', label: 'Discovery',
          count: data?.discovery.new_candidates },
        { to: '/assets/reservations', label: 'Reservations',
          count: data?.reservations?.overdue || undefined },
      ],
    },
    {
      title: 'Upkeep',
      items: [
        { to: '/assets/maintenance', label: 'Maintenance',
          count: windows },
        { to: '/assets/contracts', label: 'Support',
          count: expiring },
        { to: '/assets/parts', label: 'Parts',
          count: data?.stock?.below_reorder || undefined },
      ],
    },
    {
      title: 'Manage',
      items: [
        { to: '/assets/admin/tags', label: 'Tags' },
      ],
    },
  ];

  return (
    <div className="asset-shell">
      <nav className="asset-nav" aria-label="Asset sections">
        <h2>Assets</h2>
        {sections.map((section) => (
          <div className="asset-nav-group" key={section.title}>
            <p className="asset-nav-title">{section.title}</p>
            <ul>
              {section.items.map((item) => (
                <li key={item.to}>
                  <NavLink to={item.to} end={item.end}>
                    <span className="label">
                      {item.label}
                      {item.count ? (
                        <span className="asset-badge">{item.count}</span>
                      ) : null}
                    </span>
                  </NavLink>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>
      <div className="asset-body">
        <Outlet />
      </div>
    </div>
  );
}
