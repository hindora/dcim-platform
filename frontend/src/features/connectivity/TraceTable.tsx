import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { usePaged } from '../../components/Pagination';
import { downloadCsv, stampedName } from '../../lib/csv';
import { api, type Trace, type TraceHop, type TraceTermination }
  from '../../api/client';

/** The trace, as a table.
 *
 *  Not a fallback for the diagram - the artifact. A method statement wants
 *  "PDUA-DC1-HA-R2-01 Out-2 (C13, 10 A, L1-L2) -> PSU1 (C14)" in a form that
 *  can be pasted into a change ticket, and no picture does that. It is also
 *  the accessible reading of the same data: a node-link diagram is the hardest
 *  thing here to navigate without sight, and this is the same trace in rows.
 */

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
 *  well. Printing "unknown" down every row of a power trace teaches the reader
 *  to ignore the column, on the one layer where it would matter. */
const PORT_LAYERS = new Set(['network', 'production', 'management']);

interface Row {
  side: string;
  verdict: string;
  hop: number;
  of: number;
  h: TraceHop;
}

function rowsOf(trace: Trace): Row[] {
  const out: Row[] = [];
  for (const p of trace.paths) {
    p.hops.forEach((h, i) => out.push({
      side: p.side || '—', verdict: p.verdict,
      hop: i + 1, of: p.hops.length, h,
    }));
  }
  return out;
}

export function TraceTable({ deviceId, deviceName, layer }: {
  deviceId: string;
  deviceName: string;
  layer: string;
}) {
  const q = useQuery<Trace>({
    queryKey: ['trace', deviceId, layer],
    queryFn: () => api.trace(deviceId, layer),
    retry: false,
  });

  const rows = q.data ? rowsOf(q.data) : [];
  // `always`, because this table lives in a sheet that is 250px tall: without
  // a permanent footer the reader has no idea whether the twelve rows they can
  // scroll are the whole trace or the first page of it.
  const { rows: page, foot } = usePaged(rows, { noun: 'hops', always: true });
  const showState = PORT_LAYERS.has(layer);

  if (q.isLoading) return <div className="asset-skeleton" style={{ height: 120 }} />;
  if (q.isError) {
    return <p className="muted">That device has no {layer} connections recorded.</p>;
  }
  if (!q.data) return null;

  if (q.data.is_source) {
    return (
      <p className="muted">
        Nothing feeds <strong>{deviceName}</strong> on the {layer} layer — it is
        a source here. {q.data.downstream_count > 0 && (
          <>It feeds {q.data.downstream_count} device
            {q.data.downstream_count === 1 ? '' : 's'} directly.</>
        )}
      </p>
    );
  }

  if (!rows.length) {
    return (
      <p className="muted">
        {deviceName} has no {layer} connections recorded, so there is nothing to
        trace.
      </p>
    );
  }

  return (
    // A pane, not a stack: the head and the pager stay put and the ROWS
    // scroll. Scrolling the whole sheet put the pager below twelve rows of
    // trace, so the one control that says how much trace there is could only
    // be found by scrolling past it.
    <div className="cn-pane">
      <div className="conn-trace-head">
        <p className="muted" style={{ margin: 0 }}>
          {q.data.paths.length} chain{q.data.paths.length === 1 ? '' : 's'} to
          source, read top to bottom
          {q.data.asymmetric && (
            <span className="warn"> · the sides reach a source at different
              depths</span>
          )}
          {q.data.truncated && (
            <span className="warn"> · more cords than can be listed</span>
          )}
        </p>
        <button type="button" onClick={() => downloadCsv(
          stampedName(`trace-${deviceName}-${layer}`),
          ['Side', 'Verdict', 'Hop', 'Of', 'Feeds from', 'Out of',
           'Into', 'Feeds', 'Alternate sources',
           ...(showState ? ['State'] : [])],
          rows.map((r) => [
            r.side, r.verdict, r.hop, r.of,
            r.h.up.name, terminationLabel(r.h.up_termination),
            terminationLabel(r.h.down_termination), r.h.down.name,
            r.h.alternates.map((a) => a.name).join(' / '),
            ...(showState ? [r.h.oper_state] : []),
          ]),
        )}>Export CSV</button>
      </div>

      <div className="estate-scroll cn-pane-scroll">
        <table className="estate-table conn-trace">
          <thead>
            <tr>
              <th>Side</th>
              <th className="num">Hop</th>
              <th>Feeds from</th>
              <th>Out of</th>
              <th>Into</th>
              <th>Feeds</th>
              {showState && <th className="mid">State</th>}
            </tr>
          </thead>
          <tbody>
            {page.map((r) => (
              <tr key={`${r.side}-${r.h.connection_id}-${r.hop}`}
                  className={r.hop === r.of ? 'is-cord' : undefined}>
                <td>
                  <span className={`conn-side side-${(r.side || '').toLowerCase()}`}>
                    {r.side}
                  </span>
                </td>
                <td className="num">{r.hop}<span className="muted">/{r.of}</span></td>
                <td>
                  <Link to={`/devices/${r.h.up.id}`}>{r.h.up.name}</Link>
                  <div className="k">{r.h.up.device_type.replace(/_/g, ' ')}</div>
                  {r.h.alternates.length > 0 && (
                    // The fork lives on the hop where it happens rather than
                    // multiplying the chain out once per source.
                    <div className="k conn-alt">
                      or {r.h.alternates.map((a) => a.name).join(', ')}
                    </div>
                  )}
                </td>
                <td>{terminationLabel(r.h.up_termination)}</td>
                <td>{terminationLabel(r.h.down_termination)}</td>
                <td>
                  <Link to={`/devices/${r.h.down.id}`}>{r.h.down.name}</Link>
                  <div className="k">{r.h.down.device_type.replace(/_/g, ' ')}</div>
                </td>
                {showState && (
                  <td className="mid">
                    <span className={r.h.oper_state === 'down' ? 'warn' : 'muted'}>
                      {r.h.oper_state}
                    </span>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>

        {q.data.downstream_count > 0 && (
        <p className="muted" style={{ margin: '8px 0 0', fontSize: '0.78rem' }}>
          {deviceName} also feeds {q.data.downstream_count} device
          {q.data.downstream_count === 1 ? '' : 's'} directly
          {q.data.downstream.length < q.data.downstream_count
            && ` (first ${q.data.downstream.length} shown on the diagram)`}.
          What would break if it were removed is a different question —
          impact analysis answers it.
        </p>
        )}
      </div>

      {foot}
    </div>
  );
}
