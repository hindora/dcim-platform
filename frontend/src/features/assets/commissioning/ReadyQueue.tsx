import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  api,
  type CommissioningQueue,
  type CommissioningRow,
} from '../../../api/client';
import { usePaged } from '../../../components/Pagination';
import { Seg } from '../../../components/estate';
import { humanise, relativeTime } from '../../../lib/format';
import { LifecycleChip } from '../components/LifecycleChip';
import { SyncBar } from './SyncBar';

/** Commissioning: devices whose hardware has moved ahead of their record.
 *
 *  Not the discovery queue. Discovery answers "something is answering that
 *  appears nowhere in inventory"; everything here is already an asset, already
 *  placed and already polled, and what changed is the metal. A device the
 *  simulator has just racked will never appear as a discovery candidate, which
 *  is why this screen exists at all.
 *
 *  Every row PROPOSES a move and nothing here applies one. An OS agent
 *  answering is not evidence that anybody accepted the machine - acceptance is a
 *  cut-over with a change record and a workload owner, and a poller cannot know
 *  that happened. Confirming goes through the ordinary transition endpoint, so it
 *  is still checked against the matrix and still writes its event and audit row.
 */

const SIGNALS: Record<string, { title: string; blurb: string }> = {
  discrepancy: {
    title: 'Answering when it should not be',
    blurb: 'The record says this is gone. Either it was never pulled, or it was '
      + 'put back without anybody recording it. No transition is offered: '
      + 'somebody has to go and look.',
  },
  racked: {
    title: 'Racked',
    blurb: 'The management plane is answering for hardware the record still has '
      + 'on order or on a shelf. A BMC runs on standby power and comes up long '
      + 'before any OS, so this is the earliest evidence a box has landed.',
  },
  ready: {
    title: 'Ready to accept',
    blurb: 'Imaged, reporting, and past its soak. Accepting cuts it over into '
      + 'service, which is when its alarms stop being shelved and start paging.',
  },
};

export function ReadyQueue() {
  const [soak, setSoak] = useState('48');
  const { data, isLoading, error } = useQuery<CommissioningQueue>({
    queryKey: ['commissioning-queue', soak],
    queryFn: () => api.commissioningQueue({ soak_hours: soak }),
    refetchInterval: 60_000,
  });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;

  const items = data?.items ?? [];
  const groups = ['discrepancy', 'racked', 'ready']
    .map((sig) => [sig, items.filter((r) => r.signal === sig)] as const)
    .filter(([, rows]) => rows.length > 0);

  return (
    <>
      <h2>Commissioning</h2>
      <p className="muted">
        Devices the DCIM already knows about, whose hardware has moved ahead of
        their record. Each row proposes a transition for you to confirm — nothing
        here changes a state on its own.
      </p>

      {/* Above the queue on purpose: a device racked a minute ago is not here
          until the estate has been read again, and an empty queue with no way to
          refresh it reads as "nothing to do" when it means "nobody looked". */}
      <SyncBar />

      {/* The soak window is a knob because burn-in length is a local policy:
          24-48h is usual, and an operator chasing one machine wants to see it
          before it has finished. */}
      <div className="asset-toolbar">
        <Seg
          label="Minimum soak before acceptance"
          value={soak}
          onChange={setSoak}
          options={[
            { key: '0', label: 'Any' },
            { key: '24', label: '24h' },
            { key: '48', label: '48h' },
          ]}
        />
      </div>

      {isLoading && <p className="muted">Loading…</p>}

      {!isLoading && items.length === 0 && (
        <div className="asset-empty">
          Nothing waiting. Every device is in the state its hardware says it is.
        </div>
      )}

      {groups.map(([sig, rows]) => (
        <section key={sig} style={{ marginTop: 24 }}>
          <h3>{SIGNALS[sig].title} — {rows.length}</h3>
          <p className="muted">{SIGNALS[sig].blurb}</p>
          <QueueTable rows={rows} />
        </section>
      ))}
    </>
  );
}

function QueueTable({ rows }: { rows: CommissioningRow[] }) {
  const paged = usePaged(rows, { noun: 'devices' });
  return (
    <>
      <div className="asset-scroll">
        <table>
          <thead>
            <tr>
              <th>Device</th><th>Where</th><th>Record says</th>
              <th>Answering</th><th>Waiting</th><th>Action</th>
            </tr>
          </thead>
          <tbody>
            {paged.rows.map((r) => (
              <Row key={r.device_id} row={r} />
            ))}
          </tbody>
        </table>
      </div>
      {paged.foot}
    </>
  );
}

function Row({ row }: { row: CommissioningRow }) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const confirm = useMutation({
    mutationFn: () => api.lifecycleTransition(row.device_id, {
      to_state: row.proposed_state as string,
      // The reason a change board asks for, filled in with what was actually
      // observed rather than left blank - the whole row is the evidence.
      reason: `Confirmed from the commissioning queue: ${
        row.live_roles.join(', ') || 'endpoint'} answering`,
    }),
    onSuccess: () => {
      // The row leaves this queue and the device's own record changes, so both
      // have to be refetched.
      qc.invalidateQueries({ queryKey: ['commissioning-queue'] });
      qc.invalidateQueries({ queryKey: ['device-lifecycle', row.device_id] });
      qc.invalidateQueries({ queryKey: ['asset-summary'] });
    },
    // The API refuses an illegal move with the allowed set attached. Showing its
    // message is the point; inventing a friendlier one would hide the matrix.
    onError: (e) => setError(String(e)),
  });

  const where = [row.datacenter_code, row.room_name, row.rack_name,
    row.u_start ? `U${row.u_start}` : null].filter(Boolean).join(' · ');

  return (
    <tr>
      <td>
        <Link to={`/assets/inventory/${row.device_id}`}>{row.name}</Link>
        <div className="muted">
          {humanise(row.device_type)}
          {row.serial_number ? ` · ${row.serial_number}` : ''}
        </div>
      </td>
      <td className="muted">{where || <span className="asset-none">unplaced</span>}</td>
      <td><LifecycleChip state={row.lifecycle} /></td>
      <td className="muted">
        {row.live_roles.length > 0
          ? row.live_roles.map((x) => humanise(x.split(':')[0])).join(', ')
          : '—'}
        <div className="muted">{relativeTime(row.last_success)}</div>
      </td>
      <td className="muted">
        {row.hours_in_state == null ? '—'
          : row.hours_in_state >= 48
            ? `${Math.round(row.hours_in_state / 24)}d`
            : `${Math.round(row.hours_in_state)}h`}
      </td>
      <td>
        {row.proposed_state ? (
          <>
            <button type="button"
                    disabled={confirm.isPending}
                    onClick={() => { setError(null); confirm.mutate(); }}>
              {confirm.isPending
                ? 'Recording…'
                : `Mark ${humanise(row.proposed_state)}`}
            </button>
            {error && <div className="banner">{error}</div>}
          </>
        ) : (
          // A discrepancy has no safe button. The honest action is to open the
          // asset and find out which of the two records is wrong.
          <Link to={`/assets/inventory/${row.device_id}`}>Investigate</Link>
        )}
      </td>
    </tr>
  );
}
