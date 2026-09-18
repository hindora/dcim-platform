import { downloadCsv, stampedName } from '../../lib/csv';
import type { Trace, TraceTermination } from '../../api/client';

/** "Out-2 · C13 · 10 A · L1-L2" - only the parts that exist. An absent rating
 *  is a real state (plenty of gear is cabled without port-level detail) and it
 *  is NOT zero amps, so it prints as nothing rather than as a number. */
export function terminationLabel(t: TraceTermination): string {
  if (!t || t.type === 'none') return '—';
  const bits: string[] = [t.label || t.type];
  if (t.connector) bits.push(t.connector);
  if (t.rated_amps != null) bits.push(`${t.rated_amps} A`);
  if (t.phase) bits.push(t.phase);
  if (t.branch) bits.push(`br ${t.branch}`);
  if (t.rated_watts != null) bits.push(`${t.rated_watts} W`);
  return bits.join(' · ');
}

/** Where a conductor's operational state is a measurement rather than a blank.
 *
 *  Only ethernet reports link state. A power cord terminates on an outlet and
 *  a pipe on a stub, and neither has anything to say about whether it is "up"
 *  - which is why `alarms/link_correlation` only watches these two layers as
 *  well. Printing "unknown" down every hop of a power trace teaches the reader
 *  to ignore it, on the one layer where it would matter. */
export const PORT_LAYERS = new Set(['network', 'production', 'management']);

/** The trace as a change-ticket artifact. A method statement wants
 *  "PDUA-DC1-HA-R2-01 Out-2 (C13, 10 A, L1-L2) -> PSU1 (C14)" in a form that
 *  can be pasted, and no picture does that. One row per hop, read top to
 *  bottom from the source. */
export function exportTraceCsv(trace: Trace, deviceName: string, layer: string) {
  const showState = PORT_LAYERS.has(layer);
  downloadCsv(
    stampedName(`trace-${deviceName}-${layer}`),
    ['Side', 'Verdict', 'Hop', 'Of', 'Feeds from', 'Out of',
     'Into', 'Feeds', 'Alternate sources',
     ...(showState ? ['State'] : [])],
    trace.paths.flatMap((p) => p.hops.map((h, i) => [
      p.side || '—', p.verdict, i + 1, p.hops.length,
      h.up.name, terminationLabel(h.up_termination),
      terminationLabel(h.down_termination), h.down.name,
      h.alternates.map((a) => a.name).join(' / '),
      ...(showState ? [h.oper_state] : []),
    ])),
  );
}
