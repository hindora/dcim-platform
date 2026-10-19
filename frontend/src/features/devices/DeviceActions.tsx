import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  ApiError, api,
  type ActionPreview, type ActionStatus, type DeviceActionDef, type DeviceActions as Caps,
  type DeviceOutlet,
} from '../../api/client';
import { usePaged } from '../../components/Pagination';
import { relativeTime } from '../../lib/format';

/** Do something to the device: switch a PDU outlet, restart a server.
 *
 *  The write is made by the collector that owns the device's endpoint, over its
 *  own management protocol; this page asks for it and says how it went. Nothing
 *  here is one click from an outage:
 *
 *  - every action that takes something down is previewed first, naming what
 *    goes dark and what only loses its second feed;
 *  - those also need the device's name typed, so a slip of the mouse cannot do
 *    it and a pasted command line cannot either;
 *  - the ones that cut power outright are an admin's, and an operator sees them
 *    disabled with the reason rather than missing, so they know to ask;
 *  - the answer is waited for on screen, because "queued" is not "done".
 *
 *  A device nothing can be done to says why (a metered strip has no relay)
 *  instead of offering buttons that fail. Plant setpoints are not here by
 *  design: the building management system holds the interlocks. */

export function DeviceActions({ deviceId }: { deviceId: string }) {
  const q = useQuery<Caps>({
    queryKey: ['device-actions', deviceId],
    queryFn: () => api.deviceActions(deviceId),
    staleTime: 20_000,
  });
  const [pick, setPick] = useState<{ def: DeviceActionDef; outlet?: number } | null>(null);
  const caps = q.data;
  const outlets = useMemo(() => caps?.outlets ?? [], [caps]);
  const paged = usePaged(outlets, { noun: 'outlets' });

  if (!caps || caps.class === null) return null;
  const by = (id: string) => caps.actions.find((a) => a.id === id);

  return (
    <section>
      <h3>Actions</h3>
      {!caps.available && <p className="muted">{caps.reason}.</p>}

      {caps.available && caps.class === 'server' && (
        <div className="action-row">
          {caps.actions.map((a) => (
            <ActionButton key={a.id} def={a} onPick={() => setPick({ def: a })} />
          ))}
          <p className="muted small" style={{ flexBasis: '100%' }}>
            Through the BMC at {caps.endpoint?.protocol}, by collector {caps.endpoint?.owner}. Shut down
            and restart ask the operating system to do it cleanly; the force actions do not.
          </p>
        </div>
      )}

      {caps.available && caps.class === 'pdu' && (
        <>
          <p className="muted small">
            Outlet control over SNMP, by collector {caps.endpoint?.owner}. Switching an outlet off
            cuts power to what is corded to it: the sheet says what goes dark first.
          </p>
          <div className="asset-scroll">
            <table>
              <thead>
                <tr><th>Outlet</th><th>Corded to</th><th>If switched off</th><th /></tr>
              </thead>
              <tbody>
                {paged.rows.map((o) => <OutletRow key={o.outlet} o={o} by={by} onPick={setPick} />)}
              </tbody>
            </table>
          </div>
          {paged.foot}
        </>
      )}

      {caps.recent.length > 0 && (
        <details style={{ marginTop: 12 }}>
          <summary className="muted">Recent actions ({caps.recent.length})</summary>
          <ul className="vw-list">
            {caps.recent.map((r) => (
              <li key={r.id}>
                {r.label ?? 'action'} · <span className={r.state === 'succeeded' ? undefined : 'vw-warn'}>
                  {r.state}</span> · {r.by} · {relativeTime(r.at)}
                {r.state !== 'succeeded' && r.detail ? <span className="muted"> · {r.detail}</span> : null}
              </li>
            ))}
          </ul>
        </details>
      )}

      {pick && (
        <ActionSheet deviceId={deviceId} def={pick.def} outlet={pick.outlet}
                     onClose={() => setPick(null)} />
      )}
      <p className="muted small" style={{ marginTop: 8 }}>
        Recorded in the <Link to="/platform">audit log</Link> with who asked and why.
      </p>
    </section>
  );
}

function ActionButton({ def, onPick, small = false }: {
  def: DeviceActionDef; onPick: () => void; small?: boolean;
}) {
  return (
    <button disabled={!def.allowed} onClick={onPick}
            className={def.confirm && def.allowed ? 'danger' : undefined}
            style={small ? { padding: '4px 10px' } : undefined}
            title={def.allowed ? def.effect : `Needs ${def.role} access. ${def.effect}`}>
      {def.label}
    </button>
  );
}

function OutletRow({ o, by, onPick }: {
  o: DeviceOutlet; by: (id: string) => DeviceActionDef | undefined;
  onPick: (p: { def: DeviceActionDef; outlet: number }) => void;
}) {
  const impact = !o.load ? '–'
    : o.load.impact === 'dark' ? <span className="dev-bad">{o.load.name} loses power</span>
    : <span className="vw-warn">{o.load.name} loses redundancy</span>;
  return (
    <tr>
      <td><b>{o.outlet}</b> <span className="muted small">{o.connector}{o.branch ? ` · bank ${o.branch}` : ''}</span></td>
      <td>{o.load ? <Link to={`/devices/${o.load.id}`}>{o.load.name}</Link> : <span className="muted">nothing recorded</span>}</td>
      <td>{impact}</td>
      <td style={{ whiteSpace: 'nowrap', textAlign: 'right' }}>
        {(['outlet_on', 'outlet_off', 'outlet_cycle'] as const).map((id) => {
          const def = by(id);
          return def ? <ActionButton key={id} def={def} small onPick={() => onPick({ def, outlet: o.outlet })} /> : null;
        })}
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------- the sheet

const DONE = new Set(['succeeded', 'failed', 'expired', 'cancelled']);

function ActionSheet({ deviceId, def, outlet, onClose }: {
  deviceId: string; def: DeviceActionDef; outlet?: number; onClose: () => void;
}) {
  const qc = useQueryClient();
  const body = { action: def.id, outlet: outlet ?? null };
  const preview = useQuery<ActionPreview>({
    queryKey: ['action-preview', deviceId, def.id, outlet],
    queryFn: () => api.previewAction(deviceId, body),
    staleTime: 0, gcTime: 0, retry: false,
  });
  const [typed, setTyped] = useState('');
  const [reason, setReason] = useState('');
  const [cmd, setCmd] = useState<string | null>(null);

  const run = useMutation({
    mutationFn: () => api.runAction(deviceId, { ...body, confirm: typed, reason: reason || null }),
    onSuccess: (r) => setCmd(r.command_id),
  });

  // Wait for the collector's answer: it normally lands within a second or two.
  const status = useQuery<ActionStatus>({
    queryKey: ['action-status', deviceId, cmd],
    queryFn: () => api.actionStatus(deviceId, cmd as string),
    enabled: cmd !== null,
    refetchInterval: (qr) => (qr.state.data && DONE.has(qr.state.data.state) ? false : 1000),
  });
  const finished = status.data && DONE.has(status.data.state);
  useEffect(() => {
    if (finished) {
      qc.invalidateQueries({ queryKey: ['device-actions', deviceId] });
      qc.invalidateQueries({ queryKey: ['device-state', deviceId] });
    }
  }, [finished, qc, deviceId]);

  const p = preview.data;
  const nameOk = !def.confirm || (p != null && typed.trim().toLowerCase() === p.confirm_name.toLowerCase());
  const err = preview.error ?? run.error;

  return (
    <div className="sheet-scrim" role="dialog" aria-modal="true" aria-label={def.label}
         onClick={(e) => { if (e.target === e.currentTarget && (!cmd || finished)) onClose(); }}>
      <section className="sheet narrow">
        <header className="sheet-head">
          <div>
            <h2>{def.label}{outlet != null ? ` · outlet ${outlet}` : ''}</h2>
            <p>{def.effect}</p>
          </div>
          <button className="close" onClick={onClose} aria-label="Close"
                  disabled={cmd !== null && !finished}>✕</button>
        </header>
        <div className="sheet-body">
          {err && <div className="banner" role="alert">
            {err instanceof ApiError ? err.message : String((err as Error).message)}</div>}
          {preview.isLoading && <p className="muted">Working out what this affects…</p>}

          {p && !cmd && (
            <>
              {p.dark.length > 0 && (
                <div className="banner" role="alert">
                  <b>Goes dark:</b>
                  <ul className="vw-list">
                    {p.dark.map((d) => <li key={d.id}>{d.name} <span className="muted">· {d.why}</span></li>)}
                  </ul>
                </div>
              )}
              {p.degraded.length > 0 && (
                <div className="banner soft">
                  <b>Loses redundancy:</b>
                  <ul className="vw-list">
                    {p.degraded.map((d) => <li key={d.id}>{d.name} <span className="muted">· {d.why}</span></li>)}
                  </ul>
                </div>
              )}
              {p.note && <p className="muted">{p.note}</p>}
              <div className="form-grid">
                {def.confirm && (
                  <label>
                    <span>Type {p.confirm_name} to confirm</span>
                    <input className="mono" value={typed} autoComplete="off" spellCheck={false}
                           onChange={(e) => setTyped(e.target.value)} />
                  </label>
                )}
                <label>
                  <span>Why (kept in the audit log)</span>
                  <input value={reason} placeholder="e.g. rack move, ticket CHG-1042"
                         onChange={(e) => setReason(e.target.value)} />
                </label>
              </div>
            </>
          )}

          {cmd && (
            <div className={`banner${status.data?.state === 'succeeded' ? ' soft' : ''}`} role="status">
              {!finished && <>Waiting for the collector… <span className="muted">
                {status.data?.state === 'delivered' ? 'it has the command' : 'queued'}</span></>}
              {finished && status.data?.state === 'succeeded' && <>Done: {status.data.detail}.</>}
              {finished && status.data?.state !== 'succeeded' && (
                <>It did not go through: {status.data?.detail ?? status.data?.state}</>
              )}
            </div>
          )}
        </div>
        <div className="sheet-foot">
          <span className="spacer" />
          {(!cmd || finished) && <button onClick={onClose}>{finished ? 'Close' : 'Cancel'}</button>}
          {!cmd && (
            <button className={def.confirm ? 'danger' : 'primary'}
                    disabled={!p || !nameOk || run.isPending || !def.allowed}
                    onClick={() => run.mutate()}>
              {run.isPending ? 'Sending…' : def.label}
            </button>
          )}
        </div>
      </section>
    </div>
  );
}
