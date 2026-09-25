import type { DiscoveryCandidate } from '../../../api/client';

/** One physical machine, however many protocols it answered on.
 *
 *  A candidate is a record of one PROBE, so a server that answers SNMP on its BMC
 *  and Redfish on the same address produced two rows - and only the Redfish one
 *  carried a serial, because a BMC's SNMP agent implements no ENTITY-MIB and has no
 *  serial OID to read. Side by side, the blank SNMP row read as a defect when it
 *  was the protocol telling the truth about what it can see.
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

/** The strongest identity the rows agree on.
 *
 *  Order matters. `matched_device_id` is authority and joins rows at DIFFERENT
 *  addresses - a server's BMC and its production NIC are one machine and inventory
 *  knows it. Serial is next: it is the only key that survives a re-addressing.
 *  Address is last and weakest; it joins protocols on one interface and nothing
 *  more.
 *
 *  Deliberately NOT joined: two unmatched rows with no serial at different
 *  addresses. They may well be one machine - a BMC and its host - but nothing in
 *  the data says so, and merging them on a guess would hide a genuinely unknown
 *  responder inside a row about a different one. An audit may not do that.
 */
function keyOf(c: DiscoveryCandidate): string {
  if (c.matched_device_id) return `dev:${c.matched_device_id}`;
  const serial = (c.serial ?? '').trim().toUpperCase();
  if (serial) return `sn:${serial}`;
  if (c.address) return `addr:${c.address}`;
  return `id:${c.id}`;
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

export function collapse(items: DiscoveryCandidate[]): Responder[] {
  // Insertion-ordered, so the page keeps the order the API sorted by rather than
  // whatever order the grouping happened to discover keys in.
  const groups = new Map<string, DiscoveryCandidate[]>();
  for (const c of items) {
    const k = keyOf(c);
    const g = groups.get(k);
    if (g) g.push(c); else groups.set(k, [c]);
  }

  return [...groups].map(([key, members]) => {
    const primary = pickPrimary(members);
    // Newest first, so "which protocol reported the serial" answers about the
    // freshest reading rather than a stale one from an older run.
    const withSerial = members
      .filter((c) => (c.serial ?? '').trim())
      .sort((a, b) => String(b.last_seen ?? '').localeCompare(String(a.last_seen ?? '')));
    return {
      key,
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
