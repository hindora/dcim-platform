import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { ApiError, api, type Reservation } from '../../../api/client';
import { Dialog, DialogActions } from '../components/Dialog';

/** Held capacity: rack units and power that nothing occupies yet.
 *
 *  Utilisation is a report; a reservation is a commitment. Without one, two
 *  teams read the same free-U number and both act on it, and the conflict shows
 *  up at install time with hardware on a trolley.
 *
 *  Expired and expiring holds are pinned to the top because the failure mode of
 *  this feature everywhere it exists is a rack held for a project cancelled two
 *  years ago that nobody released.
 */
export function ReservationList() {
  const [creating, setCreating] = useState(false);
  const qc = useQueryClient();

  const { data, isLoading, error } = useQuery({
    queryKey: ['reservations'],
    queryFn: () => api.reservations({ limit: '500' }),
  });

  const release = useMutation({
    mutationFn: (id: string) => api.releaseReservation(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['reservations'] });
      qc.invalidateQueries({ queryKey: ['asset-summary'] });
      // The placeholder device goes with it, so elevations change too.
      qc.invalidateQueries({ queryKey: ['rack-elevation'] });
    },
  });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;

  const items = data?.items ?? [];
  const s = data?.summary;

  return (
    <>
      <h2>Reservations</h2>
      {s && (
        <div className="asset-tiles">
          <div className="asset-tile">
            <div className="k">Held</div>
            <div className="v">{s.held}</div>
            <div className="sub">{s.u_held}U · {Number(s.kw_held).toFixed(1)} kW</div>
          </div>
          <div className={`asset-tile${s.overdue ? ' is-gap' : ''}`}>
            <div className="k">Past their expiry</div>
            <div className="v">{s.overdue}</div>
            <div className="sub">
              {s.overdue ? 'holding space nobody has claimed' : 'none rotting'}
            </div>
          </div>
        </div>
      )}

      <p className="asset-table-note">
        <button type="button" onClick={() => setCreating(true)}>Hold capacity</button>
      </p>

      {creating && <ReservationForm onClose={() => setCreating(false)} />}

      {isLoading && <p className="muted">Loading…</p>}

      {!isLoading && items.length === 0 && (
        <div className="asset-empty">
          Nothing is held.
        </div>
      )}

      {items.length > 0 && (
        <div className="asset-scroll">
          <table>
            <thead>
              <tr>
                <th>Project</th><th>Where</th><th>Units</th><th>Power</th>
                <th>Needed by</th><th>Expires</th><th>Status</th><th />
              </tr>
            </thead>
            <tbody>
              {items.map((r) => (
                <tr key={r.id}>
                  <td>{r.project}</td>
                  <td className="muted">
                    {r.rack_id ? (
                      <Link to={`/assets/estate/racks/${r.rack_id}`}>
                        {[r.datacenter_code, r.rack_name].filter(Boolean).join(' · ')}
                      </Link>
                    ) : (
                      [r.datacenter_code, r.room_name].filter(Boolean).join(' · ') || '—'
                    )}
                  </td>
                  <td className="muted">
                    {r.u_start
                      ? `U${r.u_start}–U${r.u_start + (r.u_height ?? 1) - 1}`
                      : <span className="asset-none">room only</span>}
                  </td>
                  <td className="muted">
                    {r.power_kw != null ? `${r.power_kw} kW` : '—'}
                  </td>
                  <td className="muted">{r.needed_by ?? '—'}</td>
                  <td className={r.overdue ? 'asset-cover is-expired' : 'muted'}>
                    {r.expires_at}
                    {r.status === 'held' && (
                      <span> · {r.days_left < 0
                        ? `${Math.abs(r.days_left)}d over`
                        : `${r.days_left}d`}</span>
                    )}
                  </td>
                  <td>
                    <span className={`asset-life is-${statusClass(r)}`}>
                      {r.status}
                    </span>
                  </td>
                  <td style={{ textAlign: 'right' }}>
                    {r.status === 'held' && (
                      <button type="button" disabled={release.isPending}
                              onClick={() => release.mutate(r.id)}>
                        Release
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function statusClass(r: Reservation): string {
  if (r.status === 'held') return r.overdue ? 'expired' : 'scheduled';
  if (r.status === 'fulfilled') return 'in_service';
  return 'completed';
}

function ReservationForm({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [project, setProject] = useState('');
  const [owner, setOwner] = useState('');
  const [rackId, setRackId] = useState('');
  const [uStart, setUStart] = useState('');
  const [uHeight, setUHeight] = useState('1');
  const [powerKw, setPowerKw] = useState('');
  const [neededBy, setNeededBy] = useState('');
  const [expiresAt, setExpiresAt] = useState(inDays(90));
  const [error, setError] = useState<string | null>(null);

  const { data: racks } = useQuery({
    queryKey: ['racks', 'all'],
    queryFn: () => api.racks({ limit: '1000' }),
  });

  const save = useMutation({
    mutationFn: () => api.createReservation({
      project,
      owner_group: owner || null,
      rack_id: rackId || null,
      room_id: null,
      u_start: uStart ? Number(uStart) : null,
      u_height: uStart ? Number(uHeight) : null,
      power_kw: powerKw ? Number(powerKw) : null,
      cool_kw: null,
      needed_by: neededBy || null,
      expires_at: expiresAt,
      notes: null,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['reservations'] });
      qc.invalidateQueries({ queryKey: ['asset-summary'] });
      qc.invalidateQueries({ queryKey: ['rack-elevation'] });
      onClose();
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  });

  return (
    <Dialog title="Hold capacity" onClose={onClose}>
      <div className="asset-form">
        <label>
          <span>Project</span>
          <input value={project} autoFocus
                 onChange={(e) => setProject(e.target.value)} />
        </label>
        <label>
          <span>Owner</span>
          <input value={owner} onChange={(e) => setOwner(e.target.value)} />
        </label>
        <label className="asset-form-wide">
          <span>Rack</span>
          <select value={rackId} onChange={(e) => setRackId(e.target.value)}>
            <option value="">No specific rack — power only</option>
            {(racks?.items ?? []).map((r) => (
              <option key={r.id} value={r.id}>
                {[r.datacenter_code, r.room_name, r.name].filter(Boolean).join(' · ')}
                {r.free_u != null ? ` (${r.free_u}U free)` : ''}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>First unit</span>
          <input type="number" min="1" value={uStart} disabled={!rackId}
                 onChange={(e) => setUStart(e.target.value)} />
        </label>
        <label>
          <span>Height (U)</span>
          <input type="number" min="1" value={uHeight} disabled={!uStart}
                 onChange={(e) => setUHeight(e.target.value)} />
        </label>
        <label>
          <span>Power</span>
          <input type="number" min="0" step="0.1" value={powerKw}
                 placeholder="kW"
                 onChange={(e) => setPowerKw(e.target.value)} />
        </label>
        <label>
          <span>Needed by</span>
          <input type="date" value={neededBy}
                 onChange={(e) => setNeededBy(e.target.value)} />
        </label>
        <label>
          <span>Expires</span>
          <input type="date" value={expiresAt}
                 onChange={(e) => setExpiresAt(e.target.value)} />
        </label>
      </div>

      <p className="asset-form-note">
        {uStart
          ? 'These rack units are held until the reservation is released or fulfilled.'
          : 'No unit range: this holds power and cooling only.'}
      </p>

      {error && <div className="banner">{error}</div>}

      <DialogActions>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button"
                disabled={!project.trim() || !expiresAt || save.isPending}
                onClick={() => { setError(null); save.mutate(); }}>
          {save.isPending ? 'Holding…' : 'Hold it'}
        </button>
      </DialogActions>
    </Dialog>
  );
}

function inDays(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() + n);
  return d.toISOString().slice(0, 10);
}
