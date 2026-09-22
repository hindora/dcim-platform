import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, api, type AssetsState, type Integration } from '../../../api/client';
import { Tip } from '../../../components/HoverTip';
import { oneLine, relativeTime } from '../../../lib/format';

/** Pushing inventory into JSM Assets.
 *
 *  Three prerequisites this platform cannot do for you, stated up front
 *  rather than discovered as a failure at 02:00: Assets needs JSM Premium or
 *  Enterprise, the object schema is created by a Jira administrator, and the
 *  Imports API is authenticated by a SECOND credential — a token generated
 *  against one import source inside Jira, not the account credential this
 *  integration already holds.
 *
 *  Same shape of honesty as the webhook panel, for the same reason: a button
 *  that cannot work is worse than a sentence saying why.
 */
export function AssetsPanel({ row }: { row: Integration }) {
  const qc = useQueryClient();
  const [schemaId, setSchemaId] = useState<string | null>(null);
  const [importId, setImportId] = useState<string | null>(null);
  const [token, setToken] = useState('');

  const state = useQuery<AssetsState>({
    queryKey: ['assets-state', row.id],
    queryFn: () => api.assetsState(row.id),
  });

  const save = useMutation({
    mutationFn: () => api.configureAssets(row.id, {
      schema_id: schemaId, import_id: importId,
      import_token: token || null,
    }),
    onSuccess: () => {
      setToken('');
      qc.invalidateQueries({ queryKey: ['assets-state', row.id] });
    },
  });

  const sync = useMutation({
    mutationFn: (full: boolean) => api.syncAssets(row.id, full),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['assets-state', row.id] }),
  });

  if (state.isLoading) return <p className="muted">Loading…</p>;
  const s = state.data;
  if (!s) return <div className="banner">Could not read the export's state.</div>;

  const ready = Boolean((schemaId ?? s.schema_id) && (s.import_token_set || token));

  return (
    <div className="stack">
      <p className="muted">
        <Tip tip={oneLine(`One way, always. The DCIM is the system of record
                for physical infrastructure, and a CMDB that could write back
                into rack elevations would be a data corruption vector wearing
                a synchronisation label.`)}>
          Inventory flows from here into Assets, and never back.
        </Tip>
      </p>

      <div className="banner soft">
        <b>Three things an administrator does in Jira first.</b> Assets needs
        JSM Premium or Enterprise; the object schema is created there, not
        here; and the import token below is generated against one import
        source inside that schema — it is a different credential from the one
        this integration already holds.
      </div>

      {s.last_run_status && (
        <dl className="kv">
          <dt>Last run</dt>
          <dd>
            <span className={s.last_run_status === 'ok' ? 'muted' : 'critical'}>
              {s.last_run_status}
            </span>
            {s.last_run_at && (
              <span className="muted"> · {relativeTime(s.last_run_at)}</span>
            )}
          </dd>
          <dt>Objects pushed</dt>
          <dd>{s.objects_pushed.toLocaleString()}
            <span className="muted"> over {s.runs} run{s.runs === 1 ? '' : 's'}</span>
          </dd>
          <dt>Up to date as of</dt>
          <dd className="muted">
            {s.last_cursor ? relativeTime(s.last_cursor) : (
              <Tip tip={oneLine(`Nothing has been exported yet, so the next
                      run sends the whole estate.`)}>never</Tip>
            )}
          </dd>
          {s.last_error && (
            <>
              <dt>Last error</dt>
              <dd className="detail">{s.last_error}</dd>
            </>
          )}
        </dl>
      )}

      <div className="form-grid">
        <label>
          <span>Object schema id</span>
          <input value={schemaId ?? s.schema_id ?? ''}
                 onChange={(e) => setSchemaId(e.target.value)}
                 placeholder="7" />
          <small className="hint">
            From the schema's URL in Jira. This platform never creates one: an
            integration that could create schemas could also replace one
            another team depends on.
          </small>
        </label>

        <label>
          <span>Import source id</span>
          <input value={importId ?? s.import_id ?? ''}
                 onChange={(e) => setImportId(e.target.value)} />
          <small className="hint">For your own reference; not sent anywhere.</small>
        </label>

        <label>
          <span>Import token</span>
          <input type="password" autoComplete="off" value={token}
                 onChange={(e) => setToken(e.target.value)}
                 placeholder={s.import_token_set ? 'stored — type to replace'
                                                 : 'paste the import token'} />
          <small className="hint">
            Generated against the import source in Jira. Stored encrypted and
            never readable again.
          </small>
        </label>
      </div>

      {save.error && (
        <div className="banner">
          {save.error instanceof ApiError && save.error.status === 403
            ? 'Configuring the CMDB export needs an admin account.'
            : String((save.error as Error).message)}
        </div>
      )}

      <div className="toolbar">
        <button className="primary" disabled={save.isPending}
                onClick={() => save.mutate()}>
          {save.isPending ? 'Saving…' : 'Save'}
        </button>
        <button disabled={!ready || sync.isPending}
                onClick={() => sync.mutate(false)}>
          {sync.isPending ? 'Exporting…' : 'Export changes'}
        </button>
        <Tip tip={oneLine(`Re-sends every device rather than those changed
                since the last run. A button rather than a default, because a
                full push of a large estate will meet the Assets
                external-import rate limiter.`)}>
          <button disabled={!ready || sync.isPending}
                  onClick={() => sync.mutate(true)}>
            Full resync
          </button>
        </Tip>
      </div>

      {sync.error && (
        <div className="banner">
          {sync.error instanceof ApiError && sync.error.status === 501
            ? String(sync.error.message)
            : String((sync.error as Error).message)}
        </div>
      )}
      {sync.isSuccess && (
        <p className="muted">
          {sync.data.skipped
            ? 'Nothing has changed since the last export.'
            : `Pushed ${sync.data.pushed.toLocaleString()} objects, `
              + `${sync.data.devices_changed.toLocaleString()} of them devices.`}
        </p>
      )}

      <section className="asset-panel">
        <h3>What gets exported</h3>
        <p className="muted">
          A containment tree, not a flat device list: a Device points at its
          Rack, which points at its Room, which points at its Datacenter. That
          is what makes <code>Rack.Room.Datacenter = "DC1"</code> work in AQL.
          Telemetry and alarm state are deliberately absent — a CMDB is not a
          time series.
        </p>
        <table>
          <thead><tr><th>Object type</th><th>Attributes</th></tr></thead>
          <tbody>
            {s.schema.map((entry) => (
              <tr key={entry.type}>
                <td>{entry.type}</td>
                <td className="muted">{entry.attributes.join(', ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
