/** Paging controls for a cursor-paged list.
 *
 *  The list is cursor-paged on purpose - `(name, id) > (last name, last id)`
 *  rather than OFFSET - because under concurrent inserts OFFSET makes a page
 *  repeat or skip rows, and an inventory that quietly omits a device is worse
 *  than a slow one.
 *
 *  That has one honest consequence: you cannot jump to an arbitrary page,
 *  because page 7's cursor is only known once page 6 has been fetched. So
 *  numbered buttons appear for pages already visited, Next walks forward one at
 *  a time, and First is always available. Every other paging affordance - page
 *  size, the range, the total, knowing when you are on the last page - is here.
 */
export function Pagination({
  page, pageSize, shown, total, hasNext, onPage, onFirst, onPrev, onNext, onSize,
}: {
  page: number;
  pageSize: number;
  shown: number;
  total?: number | null;
  hasNext: boolean;
  /** Jump to a page whose cursor is already known. */
  onPage: (page: number) => void;
  onFirst: () => void;
  onPrev: () => void;
  onNext: () => void;
  onSize: (size: number) => void;
}) {
  const from = shown === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = (page - 1) * pageSize + shown;
  const pages = total != null ? Math.max(1, Math.ceil(total / pageSize)) : null;

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

      <div className="asset-pager-buttons" role="group" aria-label="Pagination">
        <button type="button" onClick={onFirst} disabled={page === 1}
                aria-label="First page">« First</button>
        <button type="button" onClick={onPrev} disabled={page === 1}
                aria-label="Previous page">‹ Prev</button>

        {/* Only pages whose cursor we hold. Offering 7 when nothing has fetched
            6 would be a button that cannot work. */}
        {Array.from({ length: page }, (_, i) => i + 1)
          .slice(Math.max(0, page - 5))
          .map((n) => (
            <button
              key={n}
              type="button"
              className={n === page ? 'is-current' : ''}
              aria-current={n === page ? 'page' : undefined}
              onClick={() => onPage(n)}
            >
              {n}
            </button>
          ))}

        <button type="button" onClick={onNext} disabled={!hasNext}
                aria-label="Next page">Next ›</button>
      </div>
    </div>
  );
}
