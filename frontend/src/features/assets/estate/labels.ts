/** Human renderings of the estate's wire-format codes.
 *
 *  "R2-02" is an identifier; a page a person reads says the word. Anything
 *  not matching the code pattern passes through untouched, so a site that
 *  names its rows and racks differently keeps its names.
 */

/** "R2" -> "Row 2". */
export function rowLabel(name: string | null | undefined): string {
  if (!name) return '—';
  const m = /^R(\d+)$/.exec(name);
  return m ? `Row ${Number(m[1])}` : name;
}

/** "R2-02" -> "Rack 02". */
export function rackLabel(name: string): string {
  const m = /^R\d+-(\d+)$/.exec(name);
  return m ? `Rack ${m[1]}` : name;
}
