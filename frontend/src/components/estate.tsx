/** Shared furniture for the estate pages.
 *
 *  Thermal, power and utilisation ask different questions of the same estate,
 *  so they share a skeleton: a headline band, a scope toggle, a dense sortable
 *  table, a pager and a CSV export. Only the columns differ, which is why the
 *  table takes a column list rather than each page rolling its own <table>.
 */
import { useMemo, useState } from 'react';

/* ------------------------------------------------------------------ headline */

export interface Kpi {
  caption: string;
  value: number | string | null;
  unit?: string;
  digits?: number;
  tone?: 'ok' | 'warn' | 'critical';
  /** Why the value is absent. Shown in the tooltip, never as a fake zero. */
  why?: string | null;
}

export function KpiBand({ items }: { items: Kpi[] }) {
  return (
    <div className="kpi-band">
      {items.map((k) => {
        const absent = k.value === null || k.value === undefined;
        const shown = absent ? '—'
          : typeof k.value === 'number'
            ? k.value.toFixed(k.digits ?? 1)
            : k.value;
        return (
          <div key={k.caption}
               className={`kpi ${absent ? 'absent' : k.tone ?? ''}`}
               title={absent ? (k.why ?? 'not measured') : undefined}>
            <span className="cap">{k.caption}</span>
            <span className="val">
              {shown}
              {!absent && k.unit && <span className="u">{k.unit}</span>}
            </span>
            <span className="rule" />
          </div>
        );
      })}
    </div>
  );
}

export function PageHead({ title, sub, kpis }: {
  title: string; sub?: React.ReactNode; kpis: Kpi[];
}) {
  return (
    <header className="estate-head">
      <div className="title">
        <h2>{title}</h2>
        {sub && <p className="sub">{sub}</p>}
      </div>
      <KpiBand items={kpis} />
    </header>
  );
}

/* ------------------------------------------------------------- small controls */

export function Seg<T extends string>({ value, options, onChange, label }: {
  value: T;
  options: { key: T; label: string }[];
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.key} type="button"
                className={o.key === value ? 'active' : ''}
                aria-pressed={o.key === value}
                onClick={() => onChange(o.key)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function ScopeTabs({ scope, onChange, roomLabel = 'ROOMS' }: {
  scope: 'sites' | 'rooms';
  onChange: (s: 'sites' | 'rooms') => void;
  roomLabel?: string;
}) {
  return (
    <div className="scope-tabs" role="tablist" aria-label="Scope">
      <button role="tab" aria-selected={scope === 'sites'}
              className={scope === 'sites' ? 'active' : ''}
              onClick={() => onChange('sites')}>SITES</button>
      <button role="tab" aria-selected={scope === 'rooms'}
              className={scope === 'rooms' ? 'active' : ''}
              onClick={() => onChange('rooms')}>{roomLabel}</button>
    </div>
  );
}

/** A change against the comparison window.
 *
 *  Three states, not two: up, down, and "there was nothing to compare with".
 *  The last one is the common case on a fresh install, and rendering it as a
 *  flat arrow would claim a stability nobody measured.
 */
export function Delta({ value, digits = 1, unit }: {
  value: number | null | undefined; digits?: number; unit?: string;
}) {
  if (value === null || value === undefined) {
    return <span className="delta none" title="no comparison window">·</span>;
  }
  const rounded = Number(value.toFixed(digits));
  if (rounded === 0) return <span className="delta flat" title="unchanged">↔</span>;
  const up = rounded > 0;
  return (
    <span className={`delta ${up ? 'up' : 'down'}`}>
      {up ? '↑' : '↓'} {up ? '+' : ''}{rounded.toFixed(digits)}{unit ?? ''}
    </span>
  );
}

/* -------------------------------------------------------------------- table */

export interface Column<Row> {
  key: string;
  label: string;
  align?: 'num' | 'mid';
  /** Sort value. Rows without one always sort last, whichever way. */
  sort?: (row: Row) => number | string | null;
  render: (row: Row) => React.ReactNode;
  width?: number;
}

export function DataTable<Row extends { id: string }>({
  rows, columns, lead, onRowClick, empty,
}: {
  rows: Row[];
  columns: Column<Row>[];
  /** Left-edge state rule per row. */
  lead?: (row: Row) => 'ok' | 'warn' | 'critical' | 'none';
  onRowClick?: (row: Row) => void;
  empty: React.ReactNode;
}) {
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [asc, setAsc] = useState(true);

  const sorted = useMemo(() => {
    const col = columns.find((c) => c.key === sortKey);
    if (!col?.sort) return rows;
    const out = [...rows];
    out.sort((a, b) => {
      const va = col.sort!(a);
      const vb = col.sort!(b);
      // Missing values sink to the bottom in both directions: a dash is not
      // "the smallest reading", it is the absence of one.
      if (va === null && vb === null) return 0;
      if (va === null) return 1;
      if (vb === null) return -1;
      const cmp = typeof va === 'number' && typeof vb === 'number'
        ? va - vb
        : String(va).localeCompare(String(vb));
      return asc ? cmp : -cmp;
    });
    return out;
  }, [rows, columns, sortKey, asc]);

  return (
    <div className="estate-scroll">
      <table className="estate-table">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} className={c.align ?? ''}
                  style={c.width ? { width: c.width, minWidth: c.width } : undefined}>
                {c.sort ? (
                  <button onClick={() => {
                    if (sortKey === c.key) setAsc(!asc);
                    else { setSortKey(c.key); setAsc(true); }
                  }}>
                    {c.label}
                    {sortKey === c.key && <span className="caret">{asc ? '▲' : '▼'}</span>}
                  </button>
                ) : c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.length === 0 && (
            <tr><td colSpan={columns.length} style={{ height: 96 }}>
              <div className="muted" style={{ textAlign: 'center' }}>{empty}</div>
            </td></tr>
          )}
          {sorted.map((row) => (
            <tr key={row.id}
                className={lead ? `lead-${lead(row)}` : undefined}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                style={onRowClick ? { cursor: 'pointer' } : undefined}>
              {columns.map((c) => (
                <td key={c.key} className={c.align ?? ''}>{c.render(row)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ------------------------------------------------------------------- footer */

export function TableFoot({ total, page, pageSize, onPage, onPageSize, onCsv }: {
  total: number; page: number; pageSize: number;
  onPage: (p: number) => void; onPageSize: (n: number) => void;
  onCsv?: () => void;
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const current = Math.min(page, pages - 1);
  return (
    <div className="estate-foot">
      <label>
        Rows{' '}
        <select value={pageSize}
                onChange={(e) => { onPageSize(Number(e.target.value)); onPage(0); }}>
          {[10, 25, 50, 100].map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
      </label>
      <span>
        {total === 0 ? 'nothing to show'
          : `${current * pageSize + 1}–${Math.min(total, (current + 1) * pageSize)} of ${total}`}
      </span>
      {onCsv && <button className="csv" onClick={onCsv}>DOWNLOAD CSV</button>}
      <span className="pager">
        <button onClick={() => onPage(0)} disabled={current === 0}>« first</button>
        <button onClick={() => onPage(current - 1)} disabled={current === 0}>‹ prev</button>
        <button onClick={() => onPage(current + 1)} disabled={current >= pages - 1}>next ›</button>
        <button onClick={() => onPage(pages - 1)} disabled={current >= pages - 1}>last »</button>
      </span>
    </div>
  );
}

export function Notes({ items }: { items: string[] }) {
  if (!items.length) return null;
  return (
    <ul className="estate-notes">
      {items.map((n) => <li key={n}>{n}</li>)}
    </ul>
  );
}

/* -------------------------------------------------------------------- modal */

export function Modal({ title, count, blurb, onClose, children }: {
  title: string; count?: number; blurb?: string;
  onClose: () => void; children: React.ReactNode;
}) {
  return (
    <div className="modal-scrim" role="dialog" aria-modal="true" aria-label={title}
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal">
        <div className="modal-head">
          <div>
            <h3>{title}{count !== undefined && <span className="count"> · {count}</span>}</h3>
            {blurb && <p>{blurb}</p>}
          </div>
          <button className="close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ helpers */

/** A number, or a dash carrying the reason it is missing. */
export function Num({ value, digits = 1, unit, why }: {
  value: number | null | undefined; digits?: number; unit?: string; why?: string | null;
}) {
  if (value === null || value === undefined) {
    return <span className="dash" title={why ?? 'not measured'}>—</span>;
  }
  return <>{value.toFixed(digits)}{unit ? <span className="why"> {unit}</span> : null}</>;
}

/** Utilisation colouring. Past 85% a constraint is close enough to bind. */
export function tone(pct: number | null | undefined): 'ok' | 'warn' | 'critical' | 'none' {
  if (pct === null || pct === undefined) return 'none';
  if (pct >= 85) return 'critical';
  if (pct >= 70) return 'warn';
  return 'ok';
}
