import type { DiscoveryRange, DiscoveryRun } from '../../../api/client';

/** The collector's own ceiling. A run past it is refused, not truncated: sweeping
 *  the first 4096 of 65,536 and reporting "found 12" would misstate what was
 *  audited. The API splits a selection across runs instead. */
export const MAX_ADDRESSES = 4096;

/** A /20 is 4094 hosts - the widest range one sweep covers. */
export const WIDEST_PREFIX = 20;

/** Seconds per address at the ceiling: a SILENT address costs the full timeout
 *  once per attempt (6s x 2 attempts) and eight run at once, so 1.5s each.
 *
 *  Calibrated against real sweeps rather than guessed. A /24 of this estate - 254
 *  addresses, 105 of them answering - took 252s and 247s on two runs. The model
 *  puts the ceiling at 381s, and the gap is the 105 that answered immediately:
 *  silence is what a sweep spends its time on.
 */
export const SECONDS_PER_ADDRESS = (6 * 2) / 8;

export const PURPOSE_LABEL: Record<string, string> = {
  it_oob: 'IT out-of-band', bms: 'BMS / facility', production: 'Production',
  other: 'Other',
};
/** Short enough for a tag in a 324px rail. */
export const PURPOSE_TAG: Record<string, string> = {
  it_oob: 'OOB', bms: 'BMS', production: 'PROD', other: 'OTHER',
};

const toInt = (octets: number[]) => octets.reduce((n, o) => n * 256 + o, 0);
const toIp = (n: number) =>
  [24, 16, 8, 0].map((s) => Math.floor(n / 2 ** s) % 256).join('.');

export type Parsed = { cidr: string; addresses: number; error?: string };

/** Check a CIDR and count what a sweep of it probes, without a round trip.
 *
 *  Minus network and broadcast below /31, which is what the sweeper skips. Host
 *  bits set is answered with the range that was meant: "10.51.11.5/24" is almost
 *  always 10.51.11.0/24 typed from a device's address.
 */
export function parseCidr(raw: string): Parsed {
  const cidr = raw.trim();
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})$/.exec(cidr);
  if (!m) return { cidr, addresses: 0, error: 'not a CIDR, e.g. 10.51.11.0/24' };
  const octets = m.slice(1, 5).map(Number);
  if (octets.some((o) => o > 255)) {
    return { cidr, addresses: 0, error: 'an octet is above 255' };
  }
  const bits = Number(m[5]);
  if (bits > 32) return { cidr, addresses: 0, error: 'prefix is above /32' };
  const size = 2 ** (32 - bits);
  const base = toInt(octets);
  if (base % size !== 0) {
    return { cidr, addresses: 0,
             error: `host bits set; the range is ${toIp(base - (base % size))}/${bits}` };
  }
  if (bits < WIDEST_PREFIX) {
    return { cidr, addresses: 0,
             error: `${size.toLocaleString()} addresses; one sweep covers a /${
               WIDEST_PREFIX} at most, so split it` };
  }
  return { cidr, addresses: bits >= 31 ? size : size - 2 };
}

/** [first, last] address of a CIDR as integers, or null if it is not one. */
export function span(cidr: string): [number, number] | null {
  const m = /^(\d+)\.(\d+)\.(\d+)\.(\d+)\/(\d+)$/.exec(cidr.trim());
  if (!m) return null;
  const base = toInt(m.slice(1, 5).map(Number));
  const size = 2 ** (32 - Number(m[5]));
  const lo = base - (base % size);
  return [lo, lo + size - 1];
}

/** Exclusions typed as a list: bare addresses are /32s, each must be inside the
 *  range. Returns the normalised list and any lines that failed. */
export function parseExclusions(text: string, cidr: string): {
  list: string[]; errors: string[];
} {
  const outer = span(cidr);
  const list: string[] = [];
  const errors: string[] = [];
  for (const raw of text.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean)) {
    const withMask = raw.includes('/') ? raw : `${raw}/32`;
    const s = span(withMask);
    if (!s) { errors.push(`${raw}: not an address or CIDR`); continue; }
    if (outer && (s[0] < outer[0] || s[1] > outer[1])) {
      errors.push(`${raw}: outside ${cidr}`);
      continue;
    }
    list.push(withMask);
  }
  return { list, errors };
}

/** What a sweep of a saved range sends to: its hosts minus its exclusions. The
 *  API collapses overlapping exclusions on save, so they can be summed. */
export function probeCount(r: Pick<DiscoveryRange, 'cidr' | 'exclusions'>): number {
  const p = parseCidr(r.cidr);
  if (p.error) return 0;
  const outer = span(r.cidr)!;
  const skipped = p.addresses === 2 ** (32 - Number(r.cidr.split('/')[1]))
    ? [] : [outer[0], outer[1]];
  let n = p.addresses;
  for (const ex of r.exclusions ?? []) {
    const s = span(ex);
    if (!s) continue;
    const size = s[1] - s[0] + 1;
    n -= size - skipped.filter((a) => a >= s[0] && a <= s[1]).length;
  }
  return Math.max(n, 0);
}

/** When a range was last looked at, and whether the look covered all of it.
 *
 *  A sweep of a /27 inside a /24 audited 30 addresses of 254. Calling the /24
 *  "swept 3 min ago" on the strength of that would be the same misstatement the
 *  address ceiling refuses to make, so a partial pass says so.
 */
export function lastSwept(cidr: string, runs: DiscoveryRun[]) {
  const mine = span(cidr);
  if (!mine) return null;
  let best: { run: DiscoveryRun; whole: boolean } | null = null;
  for (const run of runs) {
    if (run.status !== 'done') continue;
    for (const s of run.scope?.subnets ?? []) {
      const theirs = span(s);
      if (!theirs || theirs[0] > mine[1] || mine[0] > theirs[1]) continue;
      const whole = theirs[0] <= mine[0] && theirs[1] >= mine[1];
      const when = run.finished_at ?? run.started_at;
      const bestWhen = best ? best.run.finished_at ?? best.run.started_at : null;
      // Newest wins; at the same instant a whole pass beats a partial one.
      if (!best || String(when) > String(bestWhen)
          || (String(when) === String(bestWhen) && whole && !best.whole)) {
        best = { run, whole };
      }
    }
  }
  return best;
}

/** How a collector is doing, in words, or null if it is fine or unassigned.
 *
 *  The point of assigning a range to a collector is that only it can reach the
 *  range. So a run queued for a collector that is not checking in is not
 *  "pending" - it is stuck, and saying so is the difference between an operator
 *  fixing a collector and one waiting for a sweep that will never start.
 */
export function collectorTrouble(c: {
  collector_id?: string | null; collector_registered?: boolean | null;
  collector_age_s?: number | null;
}): string | null {
  if (!c.collector_id) return null;
  if (!c.collector_registered) return `${c.collector_id} has never checked in`;
  const age = c.collector_age_s ?? 0;
  if (age < 60) return null;
  const ago = age < 3600 ? `${Math.round(age / 60)} min` : age < 172800
    ? `${Math.round(age / 3600)} h` : `${Math.round(age / 86400)} days`;
  return `${c.collector_id} last checked in ${ago} ago`;
}
