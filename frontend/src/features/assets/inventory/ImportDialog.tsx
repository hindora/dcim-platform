import { useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
  ApiError,
  getToken,
  type BulkReport,
  type ImportValidation,
} from '../../../api/client';
import { Dialog, DialogActions } from '../components/Dialog';
import { humanise } from '../../../lib/format';

/** CSV import, in two phases because one phase cannot be undone.
 *
 *  An import that discovers two bad rows in four hundred at write time has
 *  already written three hundred and ninety-eight, and the operator has no way
 *  to know which. So the first pass writes nothing and reports exactly what the
 *  second would do — including which key each row matched on, so a row landing
 *  on a device by NAME when serial was expected is visible before it lands.
 *
 *  Multipart rather than the JSON client: this posts a file.
 */
export function ImportDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [validation, setValidation] = useState<ImportValidation | null>(null);
  const [report, setReport] = useState<BulkReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function post(mode: 'validate' | 'apply'): Promise<unknown> {
    const form = new FormData();
    form.append('file', file as File);
    form.append('mode', mode);
    if (mode === 'apply' && validation) form.append('digest', validation.digest);

    const token = getToken();
    const res = await fetch('/api/v1/assets/bulk/import', {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      body: form,
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = body.detail ?? body;
      throw new ApiError(res.status,
        typeof detail === 'object' && detail?.message
          ? String(detail.message)
          : JSON.stringify(detail));
    }
    return body;
  }

  async function run(mode: 'validate' | 'apply') {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const out = await post(mode);
      if (mode === 'validate') {
        setValidation(out as ImportValidation);
      } else {
        setReport(out as BulkReport);
        qc.invalidateQueries({ queryKey: ['asset-devices'] });
        qc.invalidateQueries({ queryKey: ['asset-summary'] });
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog title="Import CSV" onClose={onClose} wide={Boolean(validation)}>
      {!report && (
        <>
          <div className="asset-form">
            <label className="asset-form-wide">
              <span>File</span>
              <input
                ref={fileRef}
                type="file"
                accept=".csv,text/csv"
                onChange={(e) => {
                  setFile(e.target.files?.[0] ?? null);
                  // A new file invalidates the previous check, and the server
                  // would refuse the stale digest anyway.
                  setValidation(null);
                  setError(null);
                }}
              />
            </label>
          </div>
          <p className="asset-form-note">
            Rows are matched on external id, then serial, then asset tag, then
            name — first hit wins. Nothing is written until you have seen what
            it would do.
          </p>
        </>
      )}

      {validation && !report && (
        <>
          <div className="asset-preview-row" style={{ marginTop: 14 }}>
            <Stat n={validation.rows} label="rows read" />
            <Stat n={validation.would_update} label="would update" />
            <Stat n={validation.unmatched.length} label="unmatched"
                  tone={validation.unmatched.length ? 'warn' : undefined} />
          </div>

          <p className="muted" style={{ marginTop: 10 }}>
            Matched by:{' '}
            {Object.entries(validation.matched_by)
              .filter(([, n]) => n > 0)
              .map(([k, n]) => `${humanise(k)} ${n}`)
              .join(' · ') || 'nothing'}
          </p>

          {validation.unmatched.length > 0 && (
            <>
              <h3>Unmatched rows</h3>
              <div className="asset-scroll">
                <table>
                  <thead><tr><th>Row</th><th>Name</th><th>Why</th></tr></thead>
                  <tbody>
                    {validation.unmatched.slice(0, 25).map((u) => (
                      <tr key={u.row}>
                        <td className="muted">{u.row}</td>
                        <td>{u.name ?? <span className="asset-none">—</span>}</td>
                        <td className="muted">{u.message}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="muted">
                These are skipped. Applying updates only the {validation.would_update}{' '}
                that matched.
              </p>
            </>
          )}
        </>
      )}

      {report && (
        <>
          <p>
            <strong>{report.succeeded}</strong> updated
            {report.failed.length > 0 && <>, <strong>{report.failed.length}</strong> refused</>}.
          </p>
          {report.failed.length > 0 && (
            <div className="asset-scroll">
              <table>
                <thead><tr><th>Asset</th><th>Why</th></tr></thead>
                <tbody>
                  {report.failed.map((f) => (
                    <tr key={f.device_id}>
                      <td>{f.name ?? f.device_id}</td>
                      <td>{f.message}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {error && <div className="banner">{error}</div>}

      <DialogActions>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClose}>
          {report ? 'Close' : 'Cancel'}
        </button>
        {!validation && !report && (
          <button type="button" disabled={!file || busy}
                  onClick={() => run('validate')}>
            {busy ? 'Checking…' : 'Check the file'}
          </button>
        )}
        {validation && !report && (
          <button type="button" disabled={busy || validation.would_update === 0}
                  onClick={() => run('apply')}>
            {busy ? 'Applying…' : `Apply to ${validation.would_update}`}
          </button>
        )}
      </DialogActions>
    </Dialog>
  );
}

function Stat({ n, label, tone }: { n: number; label: string; tone?: string }) {
  return (
    <div className={`asset-stat${tone ? ` is-${tone}` : ''}`}>
      <div className="v">{n}</div>
      <div className="k">{label}</div>
    </div>
  );
}
