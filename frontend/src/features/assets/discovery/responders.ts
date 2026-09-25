import type { DiscoveryCandidate } from '../../../api/client';

/** One physical machine, however many protocols it answered on.
 *
 *  A candidate is a record of one PROBE, so a server that answers SNMP on its BMC
 *  and Redfish on the same address produced two rows - and only the Redfish one
 *  carried a serial, because a BMC's SNMP agent implements no ENTITY-MIB and has no
 *  serial OID to read. Side by side, the blank SNMP row read as a defect when it was
 *  the protocol telling the truth about what it can see.
 *
 *  So the page groups by machine and each protocol contributes what only it knows.
 */
export interface Responder {
  /** Stable across refetches, so React keys and selections survive a poll. */
  key: string;
  /** The candidate every action targets. See pickPrimary. */
  primary: DiscoveryCandidate;
  /** Every probe that reached this machine, in the order the API returned them. */
  members: DiscoveryCandidate[];
  /** Distinct protocols, upper-cased, for the badge under the address. */
  protocols: string[];
  /** Distinct addresses. More than one means the machine answers on two planes -
   *  a BMC and a production NIC - which is worth showing rather than hiding. */
  addresses: string[];
  /** The serial, from whichever probe could read one. */
  serial: string | null;
  /** Which protocol supplied it. The answer is the point: it tells an operator
   *  that the blank column on an SNMP-only row is a property of SNMP. */
  serialFrom: string | null;
}

/** Everything that identifies this probe's machine.
 *
 *  A LIST, not one best key. The first version of this returned a single key, ranked
 *  matched-device over serial over address, and that split the exact case the
 *  grouping exists for: a BMC answering SNMP with no serial and Redfish with one
 *  produced `addr:10.51.11.210` and `sn:YUGLKTRR` - one machine, one address, two
 *  rows. It only showed on UNMATCHED responders, because matched ones share a device
 *  id and joined on that, which is why 156 expected rows looked fine and the one
 *  unknown machine did not.
 *
 *  Two probes belong together if they agree on ANY of these, and agreement is
 *  transitive: a serial seen at a second address pulls that address in too, which is
 *  how a BMC and its production NIC become one row.
 */
function idsOf(c: DiscoveryCandidate): string[] {
  const ids: string[] = [];
  if (c.matched_device_id) ids.push(`dev:${c.matched_device_id}`);
  const serial = (c.serial ?? '').trim().toUpperCase();
  if (serial) ids.push(`sn:${serial}`);
  if (c.address) ids.push(`addr:${c.address}`);
  // Nothing at all to go on: keep it separate rather than merging every anonymous
  // responder into one row.
  if (ids.length === 0) ids.push(`id:${c.id}`);
  return ids;
}

/** Which member an action operates on.
 *
 *  Promotion writes the candidate's serial and identity onto the device, so
 *  promoting the SNMP half of a server would create a record with no serial - the
 *  one field that makes the device findable again after it moves. A serial-bearing
 *  member therefore wins, then one that could suggest a type (Redfish says
 *  `server` where a bare sysDescr says nothing), then the fullest description.
 *
 *  Actionable members come first regardless: if one probe has already been promoted
 *  and its sibling has not, the row still has something to do and must offer it.
 */
function pickPrimary(members: DiscoveryCandidate[]): DiscoveryCandidate {
  const actionable = members.filter((c) => c.status === 'new');
  const pool = actionable.length > 0 ? actionable : members;
  const score = (c: DiscoveryCandidate) =>
    (c.serial ? 4 : 0)
    + (c.suggested_device_type ? 2 : 0)
    + (c.suggested_vendor ? 1 : 0);
  return [...pool].sort((a, b) => {
    const d = score(b) - score(a);
    if (d !== 0) return d;
    const al = String(a.identity?.sysDescr ?? '').length;
    const bl = String(b.identity?.sysDescr ?? '').length;
    return bl - al;
  })[0];
}

const seen = <T,>(xs: (T | null | undefined)[]): T[] =>
  [...new Set(xs.filter((x): x is T => Boolean(x)))];

/** The group's name: the strongest identifier anything in it carries.
 *
 *  Not the first member's - that would change when the API returns the same probes in
 *  a different order, and the key is what React reconciles on and what a selection
 *  holds across a refetch.
 */
function keyFor(ids: Set<string>): string {
  const sorted = [...ids].sort();
  return sorted.find((i) => i.startsWith('dev:'))
    ?? sorted.find((i) => i.startsWith('sn:'))
    ?? sorted.find((i) => i.startsWith('addr:'))
    ?? sorted[0];
}

export function collapse(items: DiscoveryCandidate[]): Responder[] {
  // Union by shared identifier. Small n - a page holds hundreds of rows, not
  // millions - so a plain merge loop is clearer here than a real union-find, and
  // clarity is worth more in the thing that decides what an operator is looking at.
  const owner = new Map<string, number>();      // identifier -> group index
  const groups: { ids: Set<string>; members: DiscoveryCandidate[] }[] = [];

  for (const c of items) {
    const ids = idsOf(c);
    const hit = [...new Set(ids.map((i) => owner.get(i))
      .filter((g): g is number => g !== undefined))];

    if (hit.length === 0) {
      const g = groups.length;
      groups.push({ ids: new Set(ids), members: [c] });
      ids.forEach((i) => owner.set(i, g));
      continue;
    }

    // Merge into the lowest-indexed group so insertion order is preserved: the page
    // shows responders in the order the API sorted them.
    const [keep, ...rest] = hit.sort((a, b) => a - b);
    const target = groups[keep];
    target.members.push(c);
    ids.forEach((i) => { target.ids.add(i); owner.set(i, keep); });
    // This probe tied previously separate groups together - a serial seen at a
    // second address, say. Fold them in and leave the emptied ones behind.
    for (const other of rest) {
      groups[other].members.forEach((m) => target.members.push(m));
      groups[other].ids.forEach((i) => { target.ids.add(i); owner.set(i, keep); });
      groups[other].members = [];
      groups[other].ids = new Set();
    }
  }

  return groups.filter((g) => g.members.length > 0).map(({ ids, members }) => {
    const primary = pickPrimary(members);
    // Newest first, so "which protocol reported the serial" answers about the
    // freshest reading rather than a stale one from an older run.
    const withSerial = members
      .filter((c) => (c.serial ?? '').trim())
      .sort((a, b) => String(b.last_seen ?? '').localeCompare(String(a.last_seen ?? '')));
    return {
      key: keyFor(ids),
      primary,
      members,
      protocols: seen(members.map((c) => c.protocol?.toUpperCase())),
      addresses: seen(members.map((c) => c.address)),
      serial: withSerial[0]?.serial?.trim() || null,
      serialFrom: withSerial[0]?.protocol?.toUpperCase() ?? null,
    };
  });
}

/** Everything on this machine, or nothing.
 *
 *  Dismissing a responder has to dismiss every probe that reached it. Ignoring one
 *  protocol would drop half the row out of the queue and leave the other half
 *  still asking about the same box - and the next sweep would bring the dismissed
 *  half straight back.
 */
export const memberIds = (r: Responder): string[] => r.members.map((c) => c.id);

/** Every probe on this machine has stopped answering.
 *
 *  EVERY, not any: a server whose Redfish went quiet while its BMC still answers SNMP
 *  is not gone, it is half broken, and that belongs in front of an operator rather
 *  than filed away. A row only leaves the queue when nothing on the machine replied
 *  to a sweep that covered its address.
 */
export const isGone = (r: Responder): boolean =>
  r.members.length > 0 && r.members.every((c) => c.status === 'gone');

/** True when any probe matched inventory. */
export const isKnown = (r: Responder): boolean =>
  r.members.some((c) => c.matched_device_id);

/** The matched device, from the member that matched it - preferring a serial match,
 *  which is the stronger claim than an address one. */
export function matchOf(r: Responder): DiscoveryCandidate | null {
  const matched = r.members.filter((c) => c.matched_device_id);
  if (matched.length === 0) return null;
  return matched.find((c) => c.matched_on_serial) ?? matched[0];
}

/** Recognised, but not where inventory says it is.
 *
 *  Only knowable when the match came from a serial: on address alone a moved box
 *  reads as something new. Judged over members because the serial-bearing probe is
 *  usually not the one the address came from.
 */
export function movedFrom(r: Responder): string | null {
  const m = matchOf(r);
  if (!m?.matched_device_address) return null;
  return r.addresses.includes(m.matched_device_address)
    ? null : m.matched_device_address;
}
