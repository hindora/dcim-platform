import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { Link } from 'react-router-dom';
import { api, type Alarm, type DeviceDetail, type DeviceState, type MaintenanceWindow,
         type TopologyNode } from '../../api/client';
import { statusClass } from '../../lib/format';
import { Alarms } from './drawer/Alarms';
import { Chain } from './drawer/Chain';
import { Impact } from './drawer/Impact';
import { Members } from './drawer/Members';
import { Overview } from './drawer/Overview';
import { Ports } from './drawer/Ports';
import { SEVERITY_RANK, type DrawerCtx } from './drawer/shared';
import { PORT_LAYERS } from './trace';

export type DrawerTab = 'overview' | 'chain' | 'alarms' | 'impact' | 'ports' | 'members';

const LABEL: Record<DrawerTab, string> = {
  overview: 'Overview', chain: 'Chain', alarms: 'Alarms',
  impact: 'Impact', ports: 'Ports', members: 'Members',
};

/** What is selected: is it OK, what feeds it, what is wrong, what depends on it.
 *
 *  A drawer rather than a modal - the diagram is there to be compared against,
 *  and a modal over it hides the one thing the reader is holding in their
 *  head. Tabs rather than one long column, because a dual-fed server's two
 *  six-hop chains alone used to push everything else off the bottom.
 *
 *  The header and footer hold still; only the tab body scrolls. The tab is
 *  owned by the page, not by this component, so clicking across five PDUs
 *  keeps Impact open on each - comparing is the whole reason to click five.
 *
 *  Fetch discipline: the shell asks for the few things its own header needs
 *  (identity, state, open alarms, maintenance); each tab fetches its own data
 *  only when opened. Clicking through twenty nodes is not a hundred requests.
 */
export function Drawer({ node, layer, tab, onTab, canvasIds, onReveal,
                         onClose, onSimulate, simulating }: {
  node: TopologyNode;
  layer: string;
  tab: DrawerTab;
  onTab: (t: DrawerTab) => void;
  canvasIds: Set<string>;
  onReveal: (deviceId: string) => void;
  onClose: () => void;
  onSimulate: (deviceId: string | null) => void;
  simulating: boolean;
}) {
  // A rolled-up node is a synthetic id standing for a rack's worth of leaf
  // equipment: no device page, no chain, no impact of its own.
  const rolled = node.rolled_up > 0;

  const tabs: DrawerTab[] = rolled
    ? ['members', 'alarms']
    : ['overview', 'chain', 'alarms', 'impact',
       ...(PORT_LAYERS.has(layer) ? ['ports' as const] : [])];
  const active = tabs.includes(tab) ? tab : tabs[0];

  const detail = useQuery<DeviceDetail>({
    queryKey: ['device', node.id],
    queryFn: () => api.device(node.id),
    enabled: !rolled,
    retry: false,
  });
  const state = useQuery<DeviceState>({
    queryKey: ['device-state', node.id],
    queryFn: () => api.deviceState(node.id),
    enabled: !rolled,
    refetchInterval: 15_000,
    retry: false,
  });

  // Open alarms feed the tab badge, so they are the shell's to fetch. A rack
  // box asks for its room and keeps its own members: the endpoint filters by
  // one device or one room, and twenty per-member calls is the wrong trade.
  const alarmsQ = useQuery<{ items: Alarm[] }>({
    queryKey: ['drawer-alarms', rolled ? `room:${node.location.room_id}` : node.id],
    queryFn: () => api.alarms(rolled
      ? { room: node.location.room_id ?? undefined, include_symptoms: 'true', limit: '500' }
      : { device_id: node.id, include_symptoms: 'true', limit: '200' }),
    enabled: !rolled || Boolean(node.location.room_id),
    refetchInterval: 15_000,
    retry: false,
  });
  const deviceIds = useMemo(
    () => (rolled ? node.member_ids : [node.id]), [rolled, node.member_ids, node.id]);
  const openAlarms = useMemo(() => {
    const members = new Set(deviceIds);
    return (alarmsQ.data?.items ?? []).filter((a) => members.has(a.device_id));
  }, [alarmsQ.data, deviceIds]);

  const windowsQ = useQuery<{ items: MaintenanceWindow[] }>({
    queryKey: ['drawer-windows', node.id],
    queryFn: () => api.maintenanceWindows({ device_id: node.id }),
    enabled: !rolled,
    retry: false,
  });
  const windows = useMemo(
    () => (windowsQ.data?.items ?? []).filter(
      (w) => w.status === 'active' || w.status === 'scheduled'),
    [windowsQ.data]);
  const inWindow = windows.find((w) => w.status === 'active');

  // The impact count on the tab, once it has been asked for - not fetched just
  // to decorate a label.
  const impactCount = useQuery<{ total_cut_off: number }>({
    queryKey: ['impact', node.id],
    queryFn: () => api.impact(node.id),
    enabled: false,
  }).data?.total_cut_off;

  const [exporter, setExporter] = useState<(() => void) | null>(null);
  const setExport = useCallback(
    (fn: (() => void) | null) => setExporter(() => fn), []);
  const ctx: DrawerCtx = useMemo(
    () => ({ canvasIds, onReveal, setExport }), [canvasIds, onReveal, setExport]);

  const worst = openAlarms.reduce<string | null>((w, a) =>
    (w === null || (SEVERITY_RANK[a.severity] ?? 5) < (SEVERITY_RANK[w] ?? 5)
      ? a.severity : w), null);

  // Esc closes, unless the key was meant for a field.
  useEffect(() => {
    function onKey(e: globalThis.KeyboardEvent) {
      if (e.key !== 'Escape') return;
      const el = e.target as HTMLElement | null;
      if (el && /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) return;
      onClose();
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  // A new device starts at the top of the tab, not where the last one left it.
  const body = useRef<HTMLDivElement>(null);
  useEffect(() => { if (body.current) body.current.scrollTop = 0; }, [node.id, active]);

  const tabRefs = useRef<Partial<Record<DrawerTab, HTMLButtonElement | null>>>({});
  function onTabKey(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    const i = tabs.indexOf(active);
    const next = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
    onTab(next);
    tabRefs.current[next]?.focus();
  }

  const d = detail.data;
  const identity = rolled
    ? [`${node.rolled_up} × ${node.device_type.replace(/_/g, ' ')}`,
       node.location.rack_name, node.location.room_name]
    : [node.device_type.replace(/_/g, ' '), d?.model,
       node.location.rack_name, node.location.room_name];
  const status = rolled && node.offline_count > 0
    ? `${node.offline_count} offline` : node.status.toLowerCase();

  return (
    <aside className="conn-drawer" aria-label="Selected device">
      <header className="cd-head">
        <div>
          <h3>{node.name}</h3>
          <p className="k">{identity.filter(Boolean).join(' · ')}</p>
          <div className="cd-badges">
            <span className={`chip ${statusClass(rolled && node.offline_count
              ? 'OFFLINE' : node.status)}`}>
              <span className="dot" aria-hidden="true" />{status}
            </span>
            {openAlarms.length > 0 && (
              <button type="button" className={`cd-badge ${statusClass(worst ?? '')}`}
                      onClick={() => onTab('alarms')}>
                ▲ {openAlarms.length} alarm{openAlarms.length === 1 ? '' : 's'}
              </button>
            )}
            {windows.length > 0 && (
              <button type="button" className={`cd-badge ${inWindow ? 'warn' : ''}`}
                      onClick={() => onTab('impact')}
                      title={windows.map((w) => w.title).join('\n')}>
                ◐ {inWindow ? 'in maintenance' : 'maintenance scheduled'}
              </button>
            )}
          </div>
        </div>
        <button type="button" className="asset-max" aria-label="Close (Esc)"
                onClick={onClose}>✕</button>
      </header>

      <div className="cd-tabs" role="tablist" aria-label="Device details"
           onKeyDown={onTabKey}>
        {tabs.map((t) => {
          const count = t === 'alarms' ? openAlarms.length
            : t === 'impact' ? impactCount : undefined;
          return (
            <button key={t} type="button" role="tab" id={`cd-tab-${t}`}
                    ref={(el) => { tabRefs.current[t] = el; }}
                    aria-selected={active === t} aria-controls="cd-panel"
                    tabIndex={active === t ? 0 : -1}
                    className={active === t ? 'is-on' : undefined}
                    onClick={() => onTab(t)}>
              {LABEL[t]}
              {count ? (
                <span className={`cd-count ${t === 'alarms'
                  ? statusClass(worst ?? '') : 'critical'}`}>{count}</span>
              ) : null}
            </button>
          );
        })}
      </div>

      <div className="conn-drawer-scroll cd-body" ref={body} role="tabpanel"
           id="cd-panel" aria-labelledby={`cd-tab-${active}`}>
        {active === 'overview' && (
          <Overview node={node} layer={layer} detail={detail.data}
                    state={state.data} ctx={ctx} />
        )}
        {active === 'chain' && <Chain node={node} layer={layer} ctx={ctx} />}
        {active === 'alarms' && (
          <Alarms open={openAlarms} openLoading={alarmsQ.isLoading}
                  deviceIds={deviceIds} name={node.name}
                  roomId={node.location.room_id} ctx={ctx} />
        )}
        {active === 'impact' && (
          <Impact node={node} layer={layer} windows={windows} ctx={ctx} />
        )}
        {active === 'ports' && <Ports node={node} ctx={ctx} />}
        {active === 'members' && <Members node={node} ctx={ctx} />}
      </div>

      {/* On the drawer's floor, where they stay whichever tab is open. */}
      <div className="conn-drawer-actions">
        {!rolled && (
          // The question asked before every maintenance window.
          <button type="button" className={simulating ? 'is-on' : undefined}
                  onClick={() => onSimulate(simulating ? null : node.id)}>
            {simulating ? 'Stop simulating' : 'Simulate removal'}
          </button>
        )}
        <button type="button" disabled={!exporter}
                title={exporter ? `Export this tab as CSV` : 'Nothing on this tab to export'}
                onClick={() => exporter?.()}>
          Export
        </button>
        {rolled
          ? node.location.rack_id && (
            <Link to={`/racks/${node.location.rack_id}`}>Open rack →</Link>)
          : <Link to={`/devices/${node.id}`}>Open device →</Link>}
      </div>
    </aside>
  );
}
