import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type CoolingHop, type DeviceCooling } from '../../api/client';
import './cooling-trace.css';

const t = (v?: number | null) => (v == null ? '—' : `${v.toFixed(1)} °C`);

/** A machine's state as a word, never as colour alone. */
function stateOf(u: { running?: boolean | null; state?: string | null }): { word: string; tone: string } {
  if (u.running === false) return { word: 'stopped', tone: 'crit' };
  if (u.state && !['ok', 'unknown', 'running'].includes(u.state)) {
    return { word: u.state.replace(/_/g, ' '), tone: 'warn' };
  }
  if (u.running) return { word: 'running', tone: 'ok' };
  return { word: 'no state reported', tone: 'none' };
}

function Hop({ title, what, children }: { title: string; what: React.ReactNode; children?: React.ReactNode }) {
  return (
    <li className="ct-hop">
      <p className="ct-hop-title">{title}</p>
      <div className="ct-hop-what">{what}</div>
      {children}
    </li>
  );
}

function Unit({ u, detail }: { u: CoolingHop; detail: string }) {
  const s = stateOf(u);
  return (
    <p className="ct-unit">
      <Link to={`/devices/${u.device_id}`}>{u.name}</Link>
      <span className={`ct-state ${s.tone}`}>{s.word}</span>
      {detail && <span className="ct-num">{detail}</span>}
    </p>
  );
}

function Plant({ c }: { c: DeviceCooling }) {
  const chillers = c.plant.machines.filter((m) => m.device_type === 'chiller');
  const on = chillers.filter((m) => m.running).length;
  if (!chillers.length) return null;
  return (
    <Hop title="Chilled water" what={
      <p className="ct-unit">
        <span>{on} of {chillers.length} chillers running</span>
        <span className="ct-num">supply {t(c.plant.chw_supply_c)}</span>
      </p>}>
      {chillers.filter((m) => !m.running && m.alarms_open > 0).map((m) => (
        <p key={m.device_id} className="ct-unit">
          <Link to={`/devices/${m.device_id}`}>{m.name}</Link>
          <span className="ct-state crit">stopped with an alarm open</span>
        </p>
      ))}
    </Hop>
  );
}

export function useDeviceCooling(deviceId: string) {
  return useQuery({
    queryKey: ['device-cooling', deviceId],
    queryFn: () => api.deviceCooling(deviceId),
    refetchInterval: 60_000,
    retry: false,
  });
}

/** Where a device's heat goes, through what, and whether each machine on the
 *  way is doing its job. The cooling counterpart of the power supplies. */
export function CoolingTrace({ deviceId }: { deviceId: string }) {
  const q = useDeviceCooling(deviceId);
  if (q.isLoading) return <p className="ct-empty">Reading the cooling chain…</p>;
  if (q.isError || !q.data) return <p className="ct-empty">The cooling chain could not be read.</p>;
  const c = q.data;
  const liquid = c.method === 'liquid' && c.liquid;
  const stopped = c.air.units.filter((u) => u.running === false);
  return (
    <div className="ct">
      <p className="ct-method">
        {liquid
          ? `Direct liquid, cold plate: about ${Math.round(c.liquid!.share * 100)} % of its heat goes to `
            + 'the coolant; memory, drives and power supplies still use room air.'
          : c.method === 'air' ? 'Air: all of its heat goes to the room air.'
          : `${c.method}.`}
        {c.method_basis === 'declared' && ' (from the asset record)'}
      </p>
      {liquid && (
        <>
          <h4 className="ct-path-head">Chips</h4>
          <ol className="ct-path">
            <Hop title="Coolant loop" what={c.liquid!.cdus.length
              ? c.liquid!.cdus.map((u) => (
                <Unit key={u.device_id} u={u}
                      detail={u.supply_c != null
                        ? `supply ${t(u.supply_c)}, return ${t(u.return_c)}`
                        : u.pump_pct != null ? `pump ${Math.round(u.pump_pct)} %, no loop temperatures reported` : ''} />))
              : <p className="ct-unit">No CDU is drawn for this server.</p>} />
            <Plant c={c} />
          </ol>
        </>
      )}
      <h4 className="ct-path-head">{liquid ? 'Everything else' : 'Whole server'}</h4>
      <ol className="ct-path">
        <Hop title="Room air" what={
          <p className="ct-unit">
            <span>{c.air.running} of {c.air.units.length} cooling units running in {c.air.room_name ?? 'the room'}</span>
            <span className="ct-num">supply {t(c.air.supply_c)}</span>
          </p>}>
          {stopped.slice(0, 4).map((u) => <Unit key={u.device_id} u={u} detail="" />)}
          {stopped.length > 4 && <p className="ct-unit muted">and {stopped.length - 4} more stopped</p>}
        </Hop>
        <Plant c={c} />
      </ol>
      <p className="ct-note">
        Cooling units are tied to this server by room: every unit in the hall is taken to serve
        every rack in it until each rack's own cooling is mapped.
      </p>
    </div>
  );
}
