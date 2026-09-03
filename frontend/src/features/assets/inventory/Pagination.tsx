/** Paging controls for the inventory table.
 *
 *  Offset-paged, so any page is reachable directly. The ordering behind it is
 *  a TOTAL order - `name` then `id` - so rows cannot shuffle among themselves
 *  between fetches; what offset costs is that a row inserted or deleted earlier
 *  in the order shifts everything after it, so a reader paging through a
 *  changing estate can see one row twice or miss one. That is the accepted
 *  trade for being able to jump to page 7 without having fetched page 6.
 */
export function Pagination({
  page, pageSize, shown, total, hasNext, onPage, onSize,
}: {
  page: number;
  pageSize: number;
  shown: number;
  total?: number | null;
  hasNext: boolean;
  onPage: (page: number) => void;
  onSize: (size: number) => void;
}) {
  const from = shown === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = (page - 1) * pageSize + shown;
  const pages = total != null ? Math.max(1, Math.ceil(total / pageSize)) : null;
  const last = pages ?? page;

  return (
    <div className="asset-pager">
      <span className="asset-pager-range">
        {shown === 0
          ? 'No rows'
          : total != null
            ? `${from.toLocaleString()}–${to.toLocaleString()} of ${total.toLocaleString()}`
            : `${from.toLocaleString()}–${to.toLocaleString()}`}
        {pages != null && pages > 1 && (
          <span className="muted"> · page {page} of {pages}</span>
        )}
      </span>

      <span style={{ flex: 1 }} />

      <label className="asset-pager-size">
        <span>Rows</span>
        <select
          value={pageSize}
          onChange={(e) => onSize(Number(e.target.value))}
          aria-label="Rows per page"
        >
          {[25, 50, 100, 200].map((n) => (
            <option key={n} value={n}>{n}</option>
          ))}
        </select>
      </label>

      {pages != null && pages > 1 && (
        <label className="asset-pager-size">
          <span>Go to</span>
          {/* For twenty-seven pages the numbered buttons are enough; for two
              hundred, typing the number is the only usable way in. */}
          <input
            type="number"
            min={1}
            max={pages}
            value={page}
            aria-label="Go to page"
            onChange={(e) => {
              const n = Number(e.target.value);
              if (n >= 1 && n <= pages) onPage(n);
            }}
          />
        </label>
      )}

      <div className="asset-pager-buttons" role="group" aria-label="Pagination">
        <button type="button" onClick={() => onPage(1)} disabled={page === 1}
                aria-label="First page">«</button>
        <button type="button" onClick={() => onPage(page - 1)} disabled={page === 1}
                aria-label="Previous page">‹</button>

        {pageWindow(page, last).map((n, i) =>
          n === null ? (
            // A gap, not a button: it stands for pages nobody asked to see.
            <span className="asset-pager-gap" key={`gap-${i}`} aria-hidden="true">…</span>
          ) : (
            <button
              key={n}
              type="button"
              className={n === page ? 'is-current' : ''}
              aria-current={n === page ? 'page' : undefined}
              aria-label={`Page ${n}`}
              onClick={() => onPage(n)}
            >
              {n}
            </button>
          ))}

        <button type="button" onClick={() => onPage(page + 1)} disabled={!hasNext}
                aria-label="Next page">›</button>
        <button type="button" onClick={() => onPage(last)}
                disabled={pages == null || page === last}
                aria-label="Last page">»</button>
      </div>
    </div>
  );
}

/** The numbers to show: always the first and last, always a run around the
 *  current page, and a gap standing in for the rest.
 *
 *  Returns null where a gap belongs, so the caller renders text rather than a
 *  button somebody would try to click. */
function pageWindow(page: number, last: number): (number | null)[] {
  if (last <= 7) {
    return Array.from({ length: last }, (_, i) => i + 1);
  }
  const around = [page - 1, page, page + 1].filter((n) => n > 1 && n < last);
  const out: (number | null)[] = [1];
  if (around[0] > 2) out.push(null);
  out.push(...around);
  if (around[around.length - 1] < last - 1) out.push(null);
  out.push(last);
  return out;
}
