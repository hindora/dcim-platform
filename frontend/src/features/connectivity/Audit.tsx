import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { usePaged } from '../../components/Pagination';
import { downloadCsv, stampedName } from '../../lib/csv';
import { api, type Redundancy, type RedundancyFinding } from '../../api/client';

/** The redundancy audit.
 *
 *  The one view here that is not a picture of the wiring but a judgement about
 *  it, and the only place in the product where a COMMON MODE is visible. A
 *  load with an A cord and a B cord looks correct on its own record, on the
 *  rack elevation and in the trace table, and goes on looking correct until
 *  the RPP they both trace back to is pulled for breaker work.
 *
 *  Sorted by how rare the finding is, not by severity. A convergence one load
 *  has is a likelier mistake than one twenty share, and the ordering is the
 *  only triage this page offers - it reports, it does not grade, because
 *  whether a single-corded sensor matters is the operator's call.
 */

const KIND_LABEL: Record<string, string> = {
  converged: 'Sides meet again',
  same_side: 'Both feeds on one side',
  single_fed: 'Single fed',
};

/** The order they are worth reading in. Converged first: it is the one nothing
 *  else in this product can show. Single-fed last: it is the one most likely
 *  to be by design. */
const KINDS = ['converged', 'same_side', 'single_fed'];

export function Audit({ scope, layer, roomName, onSelectDevice }: {
  scope: string;
  layer: string;
  roomName: string;
  onSelectDevice: (deviceId: string) => void;
}) {
  const [kind, setKind] = useState<string>('all');

  const q = useQuery<Redundancy>({
    queryKey: ['redundancy', scope, layer],
    queryFn: () => api.redundancy(scope, layer),
    enabled: Boolean(scope),
    retry: false,
  });

  const all = q.data?.findings ?? [];
  const shown = kind === 'all' ? all : all.filter((f) => f.kind === kind);
  // `always`, as on the trace: the sheet shows three or four findings at a
  // time, and without a permanent range the reader cannot tell four findings
  // from the first four of forty.
  const { rows, foot } = usePaged(shown, { noun: 'findings', always: true });

  if (q.isLoading) return <div className="asset-skeleton" style={{ height: 160 }} />;
  if (q.isError) {
    return (
      <p className="muted">
        The {layer} layer has nothing to audit in this room.
      </p>
    );
  }
  if (!q.data) return null;

  if (!all.length) {
    return (
      <p className="muted">
        Nothing found. All {q.data.examined} device
        {q.data.examined === 1 ? '' : 's'} on the {layer} layer in {roomName} are
        fed the way their wiring says they should be — every load with two sides
        keeps them apart all the way up to equipment the whole estate shares.
      </p>
    );
  }

  return (
    // Same pane as the trace: the filters and the pager hold still, the
    // findings scroll between them.
    <div className="cn-pane">
      <div className="conn-trace-head">
        <div className="conn-audit-filters">
          <button type="button" className={kind === 'all' ? 'is-current' : undefined}
                  onClick={() => setKind('all')}>
            ALL <span>{all.length}</span>
          </button>
          {KINDS.filter((k) => q.data!.counts[k]).map((k) => (
            <button key={k} type="button"
                    className={kind === k ? 'is-current' : undefined}
                    onClick={() => setKind(k)}>
              <span className={`cn-key is-${k === 'converged' ? 'cut'
                : k === 'same_side' ? 'partial' : 'degraded'}`} />
              {KIND_LABEL[k]} <span>{q.data!.counts[k]}</span>
            </button>
          ))}
        </div>
        <button type="button" onClick={() => downloadCsv(
          stampedName(`redundancy-${roomName}-${layer}`),
          ['Finding', 'Device', 'Type', 'Rack', 'Room', 'Sides',
           'Shared with both sides', 'Loads sharing that point'],
          shown.map((f) => [
            KIND_LABEL[f.kind] ?? f.kind, f.device.name, f.device.device_type,
            f.device.rack_name, f.device.room_name, f.sides.join(' / '),
            f.shared.map((s) => s.name).join(' / '),
            f.shared_by || '',
          ]),
        )}>Export CSV</button>
      </div>

      <p className="muted cn-pane-note" style={{ margin: 0, fontSize: '0.78rem' }}>
        {q.data.examined} device{q.data.examined === 1 ? '' : 's'} examined on
        the {layer} layer. A finding is what the wiring IS, not a grade — plenty
        of equipment is single-corded on purpose.
      </p>

      <div className="estate-scroll cn-pane-scroll">
        <table className="estate-table conn-audit">
          <thead>
            <tr>
              <th>Finding</th>
              <th>Device</th>
              <th>Sides</th>
              <th>Both sides pass through</th>
              <th className="num">Loads sharing it</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((f) => <Row key={`${f.kind}-${f.device.id}`} f={f}
                                  onSelect={onSelectDevice} />)}
          </tbody>
        </table>
      </div>
      {foot}
    </div>
  );
}

function Row({ f, onSelect }: {
  f: RedundancyFinding;
  onSelect: (deviceId: string) => void;
}) {
  return (
    <tr>
      <td>
        <span className={`conn-kind is-${f.kind}`}>{KIND_LABEL[f.kind] ?? f.kind}</span>
        <div className="k">{f.detail}</div>
      </td>
      <td>
        {/* Selecting puts it on the diagram rather than navigating away: the
            finding is about where the device sits in a chain, and the chain is
            the thing worth looking at next. */}
        <button type="button" className="conn-linkish"
                onClick={() => onSelect(f.device.id)}>{f.device.name}</button>
        <div className="k">
          {f.device.device_type.replace(/_/g, ' ')}
          {f.device.rack_name && ` · ${f.device.rack_name}`}
        </div>
      </td>
      <td>
        {f.sides.length
          ? f.sides.map((s) => (
              <span key={s} className={`conn-side side-${s.toLowerCase()}`}>{s}</span>
            ))
          : <span className="muted">—</span>}
      </td>
      <td>
        {f.shared.length ? (
          <ul className="conn-shared">
            {f.shared.map((s) => (
              <li key={s.id}>
                <Link to={`/devices/${s.id}`}>{s.name}</Link>
                <span className="k"> {s.device_type.replace(/_/g, ' ')}</span>
              </li>
            ))}
          </ul>
        ) : <span className="muted">—</span>}
      </td>
      <td className="num">
        {f.shared_by ? f.shared_by : <span className="muted">—</span>}
      </td>
    </tr>
  );
}
