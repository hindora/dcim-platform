/** CSV export of what is on screen.
 *
 *  Built in the browser from the rows already rendered rather than fetched
 *  from a second endpoint, so the file and the table can never disagree - the
 *  export is the table, including its current sort, filter and scope.
 */

/** A text cell a spreadsheet would run as a formula.
 *
 *  Strings only. A device name, sysDescr or hostname is whatever the device - or
 *  whoever configured it - chose to report, and a responder calling itself
 *  `=HYPERLINK(...)` executes when the export is opened in Excel. A leading
 *  apostrophe makes it text. Numbers are left alone: -3.2 in a thermal export is a
 *  value, and quoting it would turn every negative delta into text.
 */
const FORMULA = /^[=+\-@\t\r]/;

/** RFC 4180 quoting: double the quotes, wrap anything that could break a row. */
function cell(value: unknown): string {
  if (value === null || value === undefined) return '';
  let s = String(value);
  if (typeof value === 'string' && FORMULA.test(s)) s = `'${s}`;
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function toCsv(headers: string[], rows: unknown[][]): string {
  return [headers.map(cell).join(','), ...rows.map((r) => r.map(cell).join(','))]
    .join('\r\n');
}

export function downloadCsv(filename: string, headers: string[], rows: unknown[][]) {
  const blob = new Blob(
    // The BOM is what makes Excel open a UTF-8 CSV as UTF-8 rather than as the
    // local ANSI codepage, which is where degree signs turn into mojibake.
    ['﻿', toCsv(headers, rows)],
    { type: 'text/csv;charset=utf-8' },
  );
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoked on the next tick: revoking synchronously can beat the download in
  // some browsers and produce an empty file.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** `thermal-2026-08-24.csv` - dated so two exports never collide in Downloads. */
export function stampedName(prefix: string, label?: string): string {
  const stamp = (label ?? new Date().toISOString().slice(0, 10)).replace(/[^\w.-]+/g, '-');
  return `${prefix}-${stamp}.csv`;
}
