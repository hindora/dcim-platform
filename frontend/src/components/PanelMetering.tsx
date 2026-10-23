import { Link } from 'react-router-dom';
import type { PanelMeter } from '../api/client';
import { relativeTime } from '../lib/format';
import './panelMetering.css';

/**
 * A device that does not meter itself - a remote power panel - read through
 * the meter clamped on it. Every figure here is the meter's and says so: the
 * panel's own status stays "not monitored", because nothing polls a cabinet
 * of breakers, and a borrowed number that passed for its own would hide the
 * day the meter stops.
 */

const kw = (w: number | null | undefined) => (w == null ? '–' : `${(w / 1000).toFixed(w >= 10_000 ? 1 : 2)} kW`);
const amps = (a: number | null | undefined) => (a == null ? '–' : `${a.toFixed(1)} A`);

/** NEMA phase imbalance: the largest deviation from the average, over the average. */
export function phaseImbalancePct(phases: Record<string, number>): number | null {
  const v = Object.values(phases);
  if (v.length < 3) return null;
  const avg = v.reduce((a, x) => a + x, 0) / v.length;
  if (avg <= 0) return null;
  return (Math.max(...v.map((x) => Math.abs(x - avg))) / avg) * 100;
}

/** What the panel's status line says: who measures it, or that nobody now does. */
export function meteringWords(meters: PanelMeter[]): { text: string; lost: boolean } | null {
  if (!meters.length) return null;
  const lost = meters.find((m) => m.stale);
  if (lost) {
    const why = lost.status !== 'ONLINE' ? `${lost.meter_name} is ${lost.status.toLowerCase()}`
      : `no reading from ${lost.meter_name}${lost.as_of ? ` since ${relativeTime(lost.as_of)}` : ''}`;
    return { text: `Not monitored · metering lost: ${why}`, lost: true };
  }
  const by = meters.length === 1 ? meters[0].meter_name : `${meters.length} meters`;
  return { text: `Not monitored · metered by ${by}`, lost: false };
}

export function PanelMetering({ meters, variant = 'page' }: {
  meters: PanelMeter[];
  /** `panel`: the 3D viewer's narrow side panel. `summary`: supply only. */
  variant?: 'page' | 'panel' | 'summary';
}) {
  return (
    <div className={`pm pm-${variant}`}>
      {meters.map((m) => {
        const imb = phaseImbalancePct(m.current_a);
        const load = m.circuits.reduce((a, c) => a + (c.power_w ?? 0), 0);
        return (
          <div key={m.meter_id} className={`pm-meter${m.stale ? ' is-lost' : ''}`}>
            <p className="pm-src">
              Read by <Link to={`/devices/${m.meter_id}`}>{m.meter_name}</Link>, the meter clamped on this panel
              {m.as_of && !m.stale && <span className="muted"> · {relativeTime(m.as_of)}</span>}
            </p>
            {m.stale && (
              <p className="pm-lost" role="status">
                Metering lost. {m.status !== 'ONLINE' ? `The meter is ${m.status.toLowerCase()}.` : 'The meter has stopped reporting.'}
                {m.as_of ? ` Last reading ${relativeTime(m.as_of)}; the figures below are that reading.` : ' No reading to show.'}
              </p>
            )}
            <dl className="pm-supply">
              <div><dt>Panel load</dt><dd>{kw(m.power_w)}</dd></div>
              {Object.keys(m.current_a).length > 0 && (
                <div><dt>Current</dt><dd>{['A', 'B', 'C'].filter((p) => p in m.current_a).map((p) => `${p} ${m.current_a[p].toFixed(1)}`).join(' · ')} A</dd></div>
              )}
              {variant !== 'summary' && Object.keys(m.voltage_ln).length > 0 && (
                <div><dt>Voltage L-N</dt><dd>{['A', 'B', 'C'].filter((p) => p in m.voltage_ln).map((p) => m.voltage_ln[p].toFixed(0)).join(' / ')} V</dd></div>
              )}
              {imb != null && (
                <div><dt>Phase imbalance</dt><dd className={imb >= 20 ? 'pm-bad' : imb >= 10 ? 'pm-warn' : undefined}>{imb.toFixed(0)} %</dd></div>
              )}
              {m.power_factor != null && <div><dt>Power factor</dt><dd>{m.power_factor.toFixed(2)}</dd></div>}
              {variant === 'page' && m.frequency_hz != null && <div><dt>Frequency</dt><dd>{m.frequency_hz.toFixed(2)} Hz</dd></div>}
              {variant === 'page' && m.voltage_thd_pct != null && <div><dt>Voltage THD</dt><dd>{m.voltage_thd_pct.toFixed(1)} %</dd></div>}
              {variant === 'page' && m.current_thd_pct != null && <div><dt>Current THD</dt><dd>{m.current_thd_pct.toFixed(1)} %</dd></div>}
              {variant !== 'summary' && m.energy_kwh != null && <div><dt>Energy</dt><dd>{Math.round(m.energy_kwh).toLocaleString()} kWh</dd></div>}
            </dl>
            {variant !== 'summary' && (
              <>
                <p className="pm-sub">
                  Branch circuits: {m.circuits.length} in use{m.spare_circuits ? `, ${m.spare_circuits} spare` : ''}
                  {m.circuits.length > 0 && <span className="muted"> · {kw(load)} on the branches</span>}
                </p>
                {m.circuits.length > 0 && (
                  <table className="pm-circuits">
                    <thead>
                      <tr><th scope="col">Circuit</th><th scope="col">Feeds</th><th scope="col" className="num">Load</th>
                        {variant === 'page' && <><th scope="col" className="num">Current</th><th scope="col" className="num">PF</th></>}</tr>
                    </thead>
                    <tbody>
                      {m.circuits.map((c) => (
                        <tr key={c.instance}>
                          <td className="mono">{c.instance}</td>
                          <td className="feeds">{c.branch_id
                            ? <Link to={`/devices/${c.branch_id}`}>{c.branch_name ?? c.label}</Link>
                            : <span className="muted">{c.label ?? 'not in the schedule'}</span>}</td>
                          <td className="num">{kw(c.power_w)}</td>
                          {variant === 'page' && <><td className="num">{amps(c.current_a)}</td>
                            <td className="num">{c.power_factor != null ? c.power_factor.toFixed(2) : '–'}</td></>}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}
