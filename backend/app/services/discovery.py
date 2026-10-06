"""Discovery: stage what answered, compare against inventory, promote on request.

The valuable half of discovery is not the sweep - it is the comparison. A list
of everything that answered is noise; "these six answered and inventory has
never heard of them" is an audit finding.

Promotion is deliberately manual. Discovery infers a device type from a
sysDescr string, which is a guess, and inventory is supposed to be a record of
fact. A sweep that created devices automatically would fill the record with
guesses and make it less trustworthy than before it ran.
"""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.repositories import discovery as repo
from app.repositories import discovery_ranges as ranges_repo
from app.services import discovery_endpoints, discovery_ranges, schedule_time
from app.services.discovery_ranges import DiscoveryError

log = get_logger("discovery")


# sysDescr fragments -> device type. Ordered, first match wins, and matched
# case-insensitively against the whole string.
#
# This is a hint for the operator promoting the candidate, never a decision.
# Vendors put almost anything in sysDescr, and two devices from the same vendor
# with different roles frequently share a description.
_TYPE_HINTS: list[tuple[str, str]] = [
    (r"\bpdu\b|power distribution", "pdu"),
    (r"\bups\b|uninterruptible", "ups"),
    (r"\bcrah\b|\bcrac\b|air handl", "crah"),
    (r"\bchiller\b", "chiller"),
    (r"\bcdu\b|coolant distribution", "cdu"),
    # Facility gear, BEFORE the network patterns, because the words collide.
    # An ASCO 7000 calls itself a "transfer switch" and the switch pattern below
    # would have filed a 4000 A transfer switch as an access switch; a LOYTEC
    # L-INX calls itself a BACnet router and the router pattern would have filed
    # it as a network router. Both are real product language rather than strings
    # this estate invented, so matching on them is reading evidence.
    (r"transfer switch|\bats\b", "ats"),
    (r"paralleling switchgear|\bswitchgear\b", "switchgear"),
    (r"motor control cent|\bmcc\b", "mcc"),
    (r"panelboard|\bmpp\b", "mpp"),
    # A gateway is worth classifying precisely because of what it does NOT tell
    # you: its agent proves the gateway is up and says nothing about the field
    # devices behind it, which are on Modbus or BACnet and invisible to a sweep.
    (r"bacnet[ /-]*(ip)?[ /-]*router|bacnet.{0,12}ms/tp", "bacnet_router"),
    (r"modbus.{0,12}gateway", "modbus_gateway"),
    # Cisco names the IMAGE in sysDescr and the image names the platform: "ISR
    # Software", "ASR1000 Software", "Catalyst L3 Switch Software (CAT9K_IOSXE)".
    # That is how a real NMS tells a router from a switch, and it has to be tried
    # before the switch pattern because both strings start "Cisco IOS Software".
    (r"\brouter\b|isr software|asr1000|asr9k|ios xr", "router"),
    # PAN-OS runs on nothing but firewalls, and a real PA-5220 says "firewall"
    # in sysDescr anyway. The OID is here as the backstop for a PAN-OS version
    # whose sysDescr wording differs.
    (r"\bfirewall\b|pan-os|1\.3\.6\.1\.4\.1\.25461(?![0-9])", "firewall"),
    # F5 is the case that needs the OID rather than the text. TMOS runs on a
    # Linux host and BIG-IP answers sysDescr with that host's uname - no
    # "BIG-IP", no "load balancer", nothing about what the box does - so a
    # collector reading only sysDescr files it as a Linux SERVER. sysObjectID is
    # the leaf that carries the answer, and it is already in the blob.
    #
    # This must stay ahead of the server patterns for that reason: the uname
    # matches `linux` and would otherwise win.
    (r"load balanc|big-ip|\btmos\b|1\.3\.6\.1\.4\.1\.3375(?![0-9])", "load_balancer"),
    # No bare "ios" here. It meant "anything running IOS is a switch", which
    # filed every Cisco IOS router in this estate as one - and the heuristic was
    # not really wrong, it was guessing, because two routers and a switch were
    # serving a byte-identical "Cisco IOS XE Software, Version 17.9.4a" and there
    # was nothing else to go on. IOS is a VENDOR signal; the vendor list has it.
    #
    # The platform images are listed because the 2960 and 1000 families name
    # themselves and nothing else - no "Catalyst", no "Switch" - so a real NMS
    # reads those two off sysObjectID or off the image name, as here.
    (r"\bswitch\b|catalyst|cat9k|cat3k|c2960|c1000 software"
    # No unanchored platform NUMBER here. "n9000" was in this list and matched
    # inside "PowerLogic ION9000", filing a revenue-grade power meter as a Nexus
    # switch. Every Nexus string carries "nx-os" anyway, so it bought nothing.
     r"|nx-os|junos|arista"
     # Dell names the network OS, not the equipment class: Enterprise SONiC
     # reports its HwSku (the platform identifier) and the N-series reports
     # "Dell EMC Networking ... DNOS". Neither says "switch", which is why both
     # families arrived with no suggested type at all.
     #
     # By NAME and not by OID on purpose. Dell's networking arc is 674.10895 and
     # 82 PowerEdge SERVERS carry 674.10895.3000, inherited from the vendor
     # fallback - so an OID backstop would file every Dell server as a switch,
     # because these patterns are tried before the server ones.
     r"|\bsonic\b|hwsku|dell emc networking|\bdnos\b"
     # Dell's networking arc, as the backstop for a Dell switch whose text says
     # nothing useful - a NOS this list has not met, or a bare agent.
     #
     # This was deliberately left out while 82 PowerEdge SERVERS carried
     # 674.10895.3000, inherited from the vendor fallback: these patterns run
     # before the server ones, so it would have filed every Dell server as a
     # switch. They now answer as net-snmp and the Microsoft SNMP service, which
     # is what runs on them, so the arc means what it claims. The guard against
     # 674.108950 is the same one every OID pattern here carries.
     r"|1\.3\.6\.1\.4\.1\.674\.10895(?![0-9])", "switch"),
    # No trailing \b after idrac or ilo: the real strings are "iDRAC9" and
    # "iLO 6", and a digit is a word character, so \bidrac\b never matched the
    # thing it was written for. Product families are listed as well, because a
    # BMC identifies itself by what it manages more reliably than by calling
    # itself a BMC. Both gaps were found by testing the exact strings the first
    # live sweep returned.
    (r"idrac|\bilo\b|xclarity|\bxcc\b|\bbmc\b", "server"),
    (r"poweredge|proliant|thinksystem|\bsys-\d", "server"),
    (r"linux|windows|\bserver\b", "server"),
    (r"sensor|transmitter|probe", "sensor"),
]

_VENDOR_HINTS: list[tuple[str, str]] = [
    (r"cisco", "Cisco"), (r"arista", "Arista"), (r"juniper|junos", "Juniper"),
    (r"dell|idrac", "Dell"), (r"hewlett|hpe|\bilo\b", "HPE"),
    (r"lenovo|\bxcc\b", "Lenovo"), (r"schneider|apc", "Schneider Electric"),
    (r"eaton", "Eaton"), (r"vertiv|liebert", "Vertiv"), (r"raritan", "Raritan"),
    # Found by the first live sweep: a Supermicro BMC classified as a server
    # but with no vendor, because the list did not have it.
    (r"supermicro", "Supermicro"),
    # Both were arriving with no vendor at all. Matched on the OID as well as
    # the name, because an F5 sysDescr contains neither.
    (r"palo alto|pan-os|1\.3\.6\.1\.4\.1\.25461(?![0-9])", "Palo Alto Networks"),
    (r"f5 networks|big-ip|\btmos\b|1\.3\.6\.1\.4\.1\.3375(?![0-9])", "F5"),
    # Facility vendors, found the way the others were: by sweeping and seeing
    # what came back with no vendor at all.
    (r"\basco\b", "ASCO Power Technologies"),
    (r"\bmoxa\b", "Moxa"),
    (r"loytec", "Loytec"),
    (r"coolit", "CoolIT Systems"),
]


#: IANA enterprise numbers -> vendor, for an SNMPv3 engine ID (RFC 3411: the
#: first four octets are 0x80000000 | the agent vendor's enterprise number).
#: net-snmp's own 8072 is left out: it names the agent software, not the box.
_ENTERPRISE_VENDORS: dict[int, str] = {
    9: "Cisco", 30065: "Arista", 2636: "Juniper", 674: "Dell", 11: "HPE",
    232: "HPE", 19046: "Lenovo", 318: "Schneider Electric", 534: "Eaton",
    476: "Vertiv", 13742: "Raritan", 10876: "Supermicro", 25461: "Palo Alto Networks",
    3375: "F5", 8691: "Moxa",
}


def engine_enterprise(engine_hex: str | None) -> int | None:
    """The enterprise number an RFC 3411 SNMPv3 engine ID carries, or None for
    a pre-RFC 3411 ID (high bit clear) or anything unreadable."""
    try:
        b = bytes.fromhex(engine_hex or "")
    except ValueError:
        return None
    if len(b) < 5 or not b[0] & 0x80:
        return None
    return int.from_bytes(bytes([b[0] & 0x7F]) + b[1:4], "big")


def classify(identity: dict[str, Any],
             protocol: str = "snmp") -> tuple[str | None, str | None]:
    """Guess a device type and vendor from what the probe could read.

    The protocol is itself evidence. Anything answering a Redfish service root is a
    management controller, and a management controller is overwhelmingly on a
    server - so that is the fallback when the text gives nothing away, which it
    often does: a bare service root carries a version and a name and no model at
    all.

    A suggestion, not a decision. Redfish does appear on some storage arrays and a
    little network gear, and the operator confirms the type on promotion - which is
    exactly why a guess here is safe and a guess written straight into inventory
    would not be.
    """
    blob = " ".join(str(v) for v in identity.values() if v).lower()
    dtype = next((t for pattern, t in _TYPE_HINTS if re.search(pattern, blob)), None) \
        if blob else None
    vendor = next((v for pattern, v in _VENDOR_HINTS if re.search(pattern, blob)), None) \
        if blob else None
    if dtype is None and protocol == "redfish":
        dtype = "server"
    if vendor is None:
        # An agent that answered USM discovery but no credential we hold: its
        # engine ID is all there is, and it still names the vendor.
        vendor = _ENTERPRISE_VENDORS.get(engine_enterprise(identity.get("engineID")) or -1)
    return dtype, vendor


#: What a run may be called. One sweep now covers every protocol the collector
#: has been configured for, so the protocol-specific name is a legacy alias.
async def create_run(session: AsyncSession, *, method: str,
                     subnets: list[str]) -> dict[str, Any]:
    """One-off subnets, to any collector. Saved ranges go through
    discovery_ranges.queue, which also routes each to the collector that can
    reach it.

    A run sweeps whatever the collector has configured - SNMP always, Redfish
    too where it is enabled - so `sweep` is the honest name. `snmp_sweep` is
    kept because runs recorded under it are in the history."""
    if not subnets:
        raise DiscoveryError("a run needs at least one subnet to sweep")
    runs = await discovery_ranges.queue(session, method=method, subnets=subnets)
    return runs[0]


def _serial_of(responder: dict[str, Any]) -> str | None:
    """The chassis serial a sweep read, if it read one.

    Lives in `identity` because that is already a free-form blob the collector
    sends, so adding it needed no change to the results contract. Normalised to
    upper case with the padding stripped: gear reports its own serial
    inconsistently, and a match that fails on a trailing space is worse than no
    match at all because it looks like a new device.
    """
    val = (responder.get("identity") or {}).get("serial")
    if not isinstance(val, str):
        return None
    return val.strip().upper() or None


#: A change here means the BOX changed. Defined beside the SQL that counts it for
#: the nav badge, so the page and the badge cannot disagree about what "replaced"
#: means.
HARDWARE_FIELDS = repo.HARDWARE_FIELDS

#: A change here is ordinary drift - firmware, OS image, a hostname. Worth being
#: able to see and date; not worth paging anybody over. A fleet-wide BMC firmware
#: roll would otherwise put three hundred rows in "needs action" overnight.
SOFT_FIELDS = ("sysDescr", "redfishVersion", "hostName", "sysName")

WATCHED_FIELDS = ("serial", "sysObjectID", "uuid", "model", "vendor", "engineID",
                  *SOFT_FIELDS)


def _over_v3(identity: dict[str, Any] | None) -> bool:
    """Did the sweep authenticate to this agent over SNMPv3?"""
    access = (identity or {}).get("access")
    return isinstance(access, dict) and str(access.get("version")) == "3"


def _norm(field: str, value: Any) -> str | None:
    if value is None:
        return None
    text_ = str(value).strip()
    if not text_:
        return None
    if field == "serial":
        return text_.upper()
    if field == "sysObjectID":
        return text_.lstrip(".")
    if field == "engineID":
        return text_.lower()
    return text_


def identity_changes(old_identity: dict[str, Any] | None, old_serial: str | None,
                     new_identity: dict[str, Any] | None, new_serial: str | None
                     ) -> list[tuple[str, str, str]]:
    """What a responder says differently from last time, field by field.

    Only where BOTH readings said something. A probe that timed out on one OID this
    run is a partial read, not a change: recording "serial: X -> nothing" would
    flap every time a slow agent dropped a varbind, and bury the real swaps.
    """
    out: list[tuple[str, str, str]] = []
    # An engine ID counts only where BOTH readings authenticated over SNMPv3: that
    # is the engine the keys are localised to and polls depend on. A v2c answer
    # from an agent that also speaks v3 carries one too, incidentally - and some
    # agents (snmpsim's shared v2c engines among them) mint a new one every start,
    # which would read as a swapped box after each restart.
    v3_both = _over_v3(old_identity) and _over_v3(new_identity)
    for field in WATCHED_FIELDS:
        if field == "engineID" and not v3_both:
            continue
        old = _norm(field, old_serial if field == "serial"
                    else (old_identity or {}).get(field))
        new = _norm(field, new_serial if field == "serial"
                    else (new_identity or {}).get(field))
        if old and new and old != new:
            out.append((field, old, new))
    return out


async def record_results(session: AsyncSession, run_id: str,
                         responders: list[dict[str, Any]]) -> dict[str, int]:
    """Stage what a sweep found and mark which of it inventory already knows.

    And what is DIFFERENT since the last look: what appeared, what went quiet, and
    what now describes itself differently - which is the question somebody reading
    last night's sweep arrives with.
    """
    addresses = [r["address"] for r in responders if r.get("address")]
    known = await repo.match_addresses(session, addresses)
    # Read BEFORE the upserts overwrite it: the previous identity is the thing a
    # change is measured against.
    prior = await repo.prior_identities(session, addresses)
    # The serial travels inside `identity`, which is already a free-form blob on
    # the wire, so reading it needs no change to the collector contract.
    by_serial = await repo.match_serials(
        session, [_serial_of(r) for r in responders])

    unmanaged, moved, appeared, changed, with_serial = 0, 0, 0, 0, 0
    changes: list[dict[str, Any]] = []
    for r in responders:
        addr = r.get("address")
        if not addr:
            continue
        identity = r.get("identity") or {}
        # Kept with the evidence, so promotion can carry the credential that
        # worked onto the record instead of asking for it again. A reference
        # ("address", "configured #2"), never a secret - see Responder.Access.
        if r.get("access"):
            identity = {**identity, "access": dict(r["access"])}
        serial = _serial_of(r)
        # SERIAL FIRST. It is the only key that survives a device being
        # re-addressed, and address matching alone reported a moved machine as
        # brand new - so promoting it created a second record for one box.
        protocol = r.get("protocol") or "snmp"
        match = by_serial.get(serial) if serial else None
        if match:
            if match.get("known_address") and match["known_address"] != addr:
                moved += 1
        else:
            match = repo.pick_match(known.get(addr), protocol)
        dtype, vendor = classify(identity, protocol)
        before = prior.get((addr, protocol))
        row = await repo.upsert_candidate(
            session, run_id=run_id, address=addr,
            protocol=protocol, identity=identity,
            matched_device_id=match["device_id"] if match else None,
            serial=serial,
            suggested_device_type=dtype, suggested_vendor=vendor)
        if not match:
            unmanaged += 1
        if serial:
            with_serial += 1
        # New to the audit, or answering again after going quiet - either way,
        # different from the last look.
        if row and (row["inserted"] or (before and before["status"] == "gone")):
            appeared += 1
        if row and before and not row["inserted"]:
            diff = identity_changes(before.get("identity"), before.get("serial"),
                                    identity, serial)
            if diff:
                changed += 1
                changes.extend({"candidate_id": row["id"], "field": f,
                                "old": o, "new": n} for f, o, n in diff)

    # AFTER the upserts, so anything this run saw has had its last_seen advanced and
    # only the genuinely silent addresses are left behind. A sweep is the only thing
    # that can tell "asked and got nothing" from "never asked", and it can only say
    # so about the subnets it actually covered.
    gone = await repo.mark_gone(session, run_id)
    await repo.record_changes(session, run_id, changes)

    counts = {"known": len(responders) - unmanaged, "unknown": unmanaged,
              "moved": moved, "with_serial": with_serial,
              "appeared": appeared, "gone": gone, "changed": changed}
    await repo.finish_run(session, run_id, found=len(responders), counts=counts)
    log.info("discovery run recorded", run_id=run_id, responders=len(responders),
             known=len(responders) - unmanaged, unmanaged=unmanaged, gone=gone,
             # Worth its own number: a device matched by serial at an address
             # inventory did not expect has MOVED, and nobody recorded it.
             readdressed=moved, appeared=appeared, changed=changed)
    return {"found": len(responders), "known": len(responders) - unmanaged,
            "unmanaged": unmanaged, "readdressed": moved, "gone": gone,
            "appeared": appeared, "changed": changed}


async def promote(session: AsyncSession, candidate_id: str,
                  payload: dict[str, Any],
                  actor: str | None = None) -> dict[str, Any]:
    """Turn a candidate into a device.

    The payload is the operator's, not the sweep's. The suggestions travel with
    the candidate so they can be accepted, but they have to be accepted - a
    sysDescr regex is not authority to create an inventory record.
    """
    cand = await repo.get_candidate(session, candidate_id)
    if cand is None:
        raise DiscoveryError(f"no candidate {candidate_id}")
    if cand["status"] != "new":
        raise DiscoveryError(
            f"candidate is already {cand['status']}; only a new one can be promoted")
    if cand["matched_device_id"]:
        raise DiscoveryError(
            "this address already belongs to a device in inventory; "
            "promoting it would create a duplicate")

    name = payload.get("name")
    device_type = payload.get("device_type") or cand["suggested_device_type"]
    if not name or not device_type:
        raise DiscoveryError("promotion needs at least a name and a device_type")

    # The vendor and model the sweep read, resolved against the catalog rather than
    # created from it. See repo.resolve_catalog for why nothing is created here.
    identity = cand["identity"] or {}
    vendor_id, model_id = await repo.resolve_catalog(
        session,
        vendor=payload.get("vendor") or cand.get("suggested_vendor"),
        model=payload.get("model") or identity.get("model")
        or cand.get("suggested_model"))
    serial = (identity.get("serial") or "").strip().upper() or None

    attach_to = payload.get("attach_to_device_id")
    if attach_to:
        result = await _attach(session, cand, candidate_id, attach_to,
                               vendor_id=vendor_id, model_id=model_id,
                               serial=serial, actor=actor)
        return await _monitor(session, cand, result, payload,
                              device_type=result.pop("device_type"))

    # `installed`, not `in_service`. A sweep found a box answering on the
    # management network, which is evidence somebody RACKED it and nothing more.
    # Accepting it into service is a cut-over with a change record and a workload
    # owner, and promoting a candidate is not that decision.
    #
    # Landing on `in_service` also skipped the commissioning queue entirely - the
    # device would arrive already live, already paging, with nobody having looked
    # at it. Now it appears there as "ready to accept" and somebody presses the
    # button, which is the whole point of that screen.
    row = (await session.execute(text("""
        INSERT INTO device (name, device_type, mgmt_ip, lifecycle, attributes,
                            vendor_id, model_id, serial_number)
        VALUES (:name, :dtype, CAST(:ip AS inet), 'installed',
                CAST(:attrs AS jsonb),
                CAST(:vendor AS uuid), CAST(:model AS uuid), :serial)
        RETURNING id::text, name
    """), {
        "name": name, "dtype": device_type, "ip": cand["address"],
        "vendor": vendor_id, "model": model_id, "serial": serial,
        # Keep the evidence. Six months from now the question "why is this
        # device recorded as a switch" has an answer.
        "attrs": json.dumps({
            "discovered": True,
            "discovery_candidate_id": candidate_id,
            "discovery_identity": cand["identity"],
        }),
    })).mappings().first()

    # The first lifecycle event, so the device has a history from the moment it
    # enters inventory - and so the commissioning queue's soak clock has something
    # to measure from. Without it the clock falls back to `updated_at`, which any
    # later edit would reset.
    await session.execute(text("""
        INSERT INTO device_lifecycle_event (device_id, from_state, to_state,
                                            reason, actor)
        VALUES (CAST(:id AS uuid), NULL, 'installed', :reason, :actor)
    """), {"id": row["id"], "actor": actor or "discovery",
           "reason": f"promoted from a discovery sweep; answered at "
                     f"{cand['address']}"})

    await repo.set_candidate_status(session, candidate_id, "promoted")
    log.info("candidate promoted", candidate_id=candidate_id,
             device_id=row["id"], name=row["name"])
    return await _monitor(session, cand, {"device_id": row["id"], "name": row["name"]},
                          payload, device_type=device_type)


async def _monitor(session: AsyncSession, cand: dict[str, Any],
                   result: dict[str, Any], payload: dict[str, Any], *,
                   device_type: str) -> dict[str, Any]:
    """Wire up how the new record will be polled, and settle its other probes.

    The endpoints are the operator's choice from the dialog (`endpoints`), or -
    for a bulk promote, which cannot ask - whatever the sweep's evidence alone
    supports (`auto_endpoints`). A device promoted with no endpoint is still a
    valid record; it just waits on the Commissioning page with nothing to soak.
    """
    vendor = cand.get("suggested_vendor") or (cand.get("identity") or {}).get("vendor")
    requests = payload.get("endpoints")
    skipped: list[dict[str, Any]] = []
    if requests is None and payload.get("auto_endpoints"):
        requests = []
        for item in await discovery_endpoints.plan(
                session, cand, device_type=device_type, vendor=vendor):
            if item["suggested_credential"]:
                requests.append({"candidate_id": item["candidate_id"],
                                 "credential": item["suggested_credential"]})
            else:
                skipped.append({"protocol": item["protocol"],
                                "address": item["address"],
                                "reason": item["credential_note"]})
    try:
        made = await discovery_endpoints.create(
            session, device_id=result["device_id"], device_type=device_type,
            vendor=vendor, cand=cand, requests=requests or [])
    except discovery_endpoints.EndpointPlanError as exc:
        raise DiscoveryError(str(exc)) from None
    await discovery_endpoints.settle_probes(session, cand, result["device_id"])
    return {**result, "endpoints": made, "endpoints_skipped": skipped}


async def cancel_run(session: AsyncSession, run_id: str,
                     actor: str | None) -> dict[str, Any]:
    """Stop a sweep that is queued or running.

    Queued: it never starts. Running: the collector cannot be interrupted - it
    does not listen while it sweeps - so it finishes, and what it reports is
    discarded. Either way the run stops counting as in flight, which is what a
    run stuck on a collector that never checks in was doing to every schedule:
    a due schedule waits while any sweep is in flight.
    """
    who = actor or "an operator"
    was = await repo.cancel_run(
        session, run_id, reason=f"cancelled by {who} before a collector took it",
        running_reason=f"cancelled by {who} while running; its results are discarded")
    if was is None:
        status = await repo.lock_run_status(session, run_id)
        if status is None:
            raise DiscoveryError("no such sweep")
        raise DiscoveryError(f"that sweep already finished ({status}); only a "
                             f"queued or running one can be cancelled")
    log.info("discovery run cancelled", run_id=run_id, was=was["was"],
             collector=was["collector_id"], actor=who)
    return was


async def monitoring_plan(session: AsyncSession, candidate_id: str,
                          device_type: str | None) -> dict[str, Any]:
    """What promoting this candidate would wire up, for the dialog to show."""
    cand = await repo.get_candidate(session, candidate_id)
    if cand is None:
        raise DiscoveryError(f"no candidate {candidate_id}")
    dtype = device_type or cand.get("suggested_device_type") or ""
    vendor = cand.get("suggested_vendor") or (cand.get("identity") or {}).get("vendor")
    return {"device_type": dtype,
            "endpoints": await discovery_endpoints.plan(
                session, cand, device_type=dtype, vendor=vendor)}


async def _attach(session: AsyncSession, cand: dict[str, Any], candidate_id: str,
                  device_id: str, *, vendor_id: str | None, model_id: str | None,
                  serial: str | None, actor: str | None) -> dict[str, Any]:
    """Fulfil a reservation with the hardware that has turned up.

    This is the join the onboarding path was missing. A capacity request creates a
    `planned` placeholder holding rack units and power; the box arrives, an engineer
    racks it, a sweep finds it - and promotion used to INSERT, leaving two records
    for one machine: the placeholder still holding the slot, and a discovered device
    with no placement at all.

    Attaching keeps the placement, because that is the half discovery cannot know -
    a sweep has no idea which rack a responder is in - and takes the address,
    serial and identity from the wire, which is the half the reservation could not
    know. Neither source is overruled: each fills in what only it has.

    The operator chooses the target. There is no key to match on: a placeholder has
    no address and no serial, which is the whole reason it is a placeholder.
    """
    target = (await session.execute(text("""
        SELECT id::text, name, lifecycle::text AS lifecycle, device_type,
               host(mgmt_ip) AS mgmt_ip, serial_number
          FROM device WHERE id = CAST(:id AS uuid)
    """), {"id": device_id})).mappings().first()
    if target is None:
        raise DiscoveryError(f"no device {device_id}")
    if target["lifecycle"] not in ("planned", "in_stock"):
        raise DiscoveryError(
            f"{target['name']} is {target['lifecycle']}, so it is not hardware "
            f"anybody is waiting for; only a planned or in_stock record can be "
            f"fulfilled by a responder")
    if target["mgmt_ip"]:
        raise DiscoveryError(
            f"{target['name']} already answers at {target['mgmt_ip']}; attaching "
            f"this responder would move a record that is already placed")
    # A serial on both that disagrees means this is not that box. Refusing beats
    # silently overwriting the serial an operator typed off a delivery note.
    if (serial and target["serial_number"]
            and target["serial_number"].strip().upper() != serial):
        raise DiscoveryError(
            f"{target['name']} is recorded with serial {target['serial_number']} "
            f"and this responder reports {serial}; they are not the same machine")

    await session.execute(text("""
        UPDATE device
           SET mgmt_ip = CAST(:ip AS inet),
               lifecycle = 'installed',
               serial_number = COALESCE(:serial, serial_number),
               vendor_id = COALESCE(CAST(:vendor AS uuid), vendor_id),
               model_id = COALESCE(CAST(:model AS uuid), model_id),
               attributes = attributes || CAST(:attrs AS jsonb),
               updated_at = now()
         WHERE id = CAST(:id AS uuid)
    """), {"id": device_id, "ip": cand["address"], "serial": serial,
           "vendor": vendor_id, "model": model_id,
           "attrs": json.dumps({
               "discovered": True,
               "discovery_candidate_id": candidate_id,
               "discovery_identity": cand["identity"],
           })})

    await session.execute(text("""
        INSERT INTO device_lifecycle_event (device_id, from_state, to_state,
                                            reason, actor)
        VALUES (CAST(:id AS uuid), CAST(:from AS lifecycle_t), 'installed',
                :reason, :actor)
    """), {"id": device_id, "from": target["lifecycle"],
           "actor": actor or "discovery",
           "reason": f"fulfilled by a discovered responder at {cand['address']}"})

    await repo.set_candidate_status(session, candidate_id, "promoted")
    log.info("candidate attached to a reservation", candidate_id=candidate_id,
             device_id=device_id, name=target["name"],
             was=target["lifecycle"])
    return {"device_id": device_id, "name": target["name"], "attached": True,
            "device_type": target["device_type"]}


async def ignore(session: AsyncSession, candidate_id: str) -> dict[str, Any]:
    row = await repo.set_candidate_status(session, candidate_id, "ignored")
    if row is None:
        raise DiscoveryError(f"no candidate {candidate_id}")
    return row


async def unignore(session: AsyncSession, candidate_id: str) -> dict[str, Any]:
    """Put a dismissed responder back in the queue.

    Ignore was one-way and invisible: a responder dismissed by mistake left the
    audit for good, and "what have we decided not to look at" is itself a question
    worth being able to answer - a device somebody waved away is exactly where an
    unmanaged box hides.

    Only from `ignored`. A promoted candidate is a device now, and dragging it
    back to `new` would offer to promote it a second time.
    """
    cand = await repo.get_candidate(session, candidate_id)
    if cand is None:
        raise DiscoveryError(f"no candidate {candidate_id}")
    if cand["status"] != "ignored":
        raise DiscoveryError(
            f"candidate is {cand['status']}, not ignored; only a dismissed one "
            f"can be restored")
    row = await repo.set_candidate_status(session, candidate_id, "new")
    log.info("candidate restored", candidate_id=candidate_id,
             address=cand.get("address"))
    return row



async def missing(session: AsyncSession) -> list[dict[str, Any]]:
    """Devices on record that the last sweep of their range did not hear from."""
    return await repo.missing_devices(session)


async def acknowledge(session: AsyncSession, candidate_ids: list[str],
                      actor: str | None) -> int:
    """Somebody has looked at what changed and agrees it was expected."""
    return await repo.acknowledge_changes(session, candidate_ids, actor)


#: The intervals a range is actually swept on. A free integer invites "every 1
#: hour" on a /16, which queues sweeps faster than one can finish.
SCHEDULE_INTERVALS = (6, 12, 24, 48, 168)


_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _validate_range_ids(range_ids: list[str]) -> list[str]:
    """A schedule sweeps saved ranges, not CIDR text: editing a range then
    changes what its schedules audit, instead of leaving them on a stale copy."""
    out = list(dict.fromkeys(str(r).strip() for r in range_ids or [] if r))
    if not out:
        raise DiscoveryError("a schedule needs at least one range")
    for r in out:
        if not _UUID.fullmatch(r):
            raise DiscoveryError(f"{r!r} is not a range id")
    return out


async def _existing_ranges(session: AsyncSession, ids: list[str]) -> list[dict[str, Any]]:
    found = await ranges_repo.get_ranges(session, ids)
    if len(found) != len(ids):
        raise DiscoveryError("a chosen range no longer exists; reload and try again")
    return found


def _timing(run_at: Any, days: Any, tz: Any, interval_hours: Any) -> dict[str, Any]:
    try:
        return schedule_time.timing(run_at, days, tz, interval_hours, SCHEDULE_INTERVALS)
    except schedule_time.ScheduleTimeError as exc:
        raise DiscoveryError(str(exc)) from None


async def create_schedule(session: AsyncSession, *, name: str | None,
                          range_ids: list[str], interval_hours: int | None = 24,
                          first_run_at: Any | None = None,
                          actor: str | None = None, run_at: Any = None,
                          days: Any = None, timezone: Any = None) -> dict[str, Any]:
    """An interval schedule runs first NOW; a timed one at its first slot."""
    ids = _validate_range_ids(range_ids)
    t = _timing(run_at, days, timezone, interval_hours)
    ranges = await _existing_ranges(session, ids)
    label = (name or "").strip() or ", ".join(r["name"] for r in ranges)
    if first_run_at is None and t["run_at"]:
        first_run_at = schedule_time.next_after(t, datetime.now(UTC))
    return await repo.create_schedule(session, name=label, range_ids=ids,
                                      interval_hours=t["interval_hours"],
                                      first_run_at=first_run_at, actor=actor,
                                      run_at=t["run_at"], days=t["days"],
                                      timezone=t["timezone"])


async def update_schedule(session: AsyncSession, schedule_id: str,
                          fields: dict[str, Any]) -> dict[str, Any]:
    current = await repo.get_schedule(session, schedule_id)
    if current is None:
        raise DiscoveryError(f"no schedule {schedule_id}")
    if "range_ids" in fields:
        fields["range_ids"] = _validate_range_ids(fields["range_ids"])
        await _existing_ranges(session, fields["range_ids"])
    # Re-timed: validated as a whole, and the next run moves to the new timing -
    # otherwise a schedule changed from "daily" to "weekdays at 02:00" would fire
    # once more at its old time first.
    if {"run_at", "days", "timezone", "interval_hours"} & fields.keys():
        merged = {**current, **fields}
        t = _timing(merged.get("run_at"), merged.get("days"),
                    merged.get("timezone"), merged.get("interval_hours"))
        fields.update(t)
        fields.setdefault("next_run_at",
                          schedule_time.next_after(t, datetime.now(UTC)).isoformat())
    row = await repo.update_schedule(session, schedule_id, fields)
    if row is None:
        raise DiscoveryError(f"no schedule {schedule_id}")
    return row


async def fire_due_schedule(session: AsyncSession) -> dict[str, Any] | None:
    """Queue ONE due schedule's sweep, if its collectors are free.

    Free per collector, not estate-wide: a collector sweeps one run at a time and
    its network's agents answer through the same listeners, so two sweeps there
    do not finish faster, they time each other out. A different collector's sweep
    is no reason to wait. A due schedule whose collector is busy stays due and
    fires on a later tick - late, rather than stacked on the sweep in flight -
    and the next due schedule gets its turn meanwhile.
    """
    for sched in await repo.claim_due_schedules(session):
        # Its ranges as they are NOW. A disabled one is skipped rather than
        # failing the schedule: somebody paused that range, not the whole audit.
        ranges = await ranges_repo.get_ranges(session, list(sched["range_ids"]))
        live = [r for r in ranges if r["enabled"]]
        if not live:
            # Advanced anyway, or it would be "due" on every tick for ever.
            await repo.advance_schedule(session, sched["id"], None,
                                        schedule_time.next_after(sched, datetime.now(UTC)))
            log.warning("scheduled sweep skipped: no enabled ranges",
                        schedule=sched["name"])
            continue
        # A change freeze skips the ranges it covers - recorded as a skipped
        # run so the history says why - and the rest of the schedule sweeps.
        blackouts = await ranges_repo.active_blackouts(session)
        frozen = [(r, discovery_ranges.frozen_by(blackouts, r.get("datacenter_id")))
                  for r in live]
        blocked = [(r, b) for r, b in frozen if b]
        live = [r for r, b in frozen if not b]
        skipped_id = None
        if blocked:
            b = blocked[0][1]
            skipped_id = await repo.record_skipped_run(
                session, scope={"subnets": [r["cidr"] for r, _ in blocked]},
                schedule_id=sched["id"], schedule_label=sched["name"],
                collector_id=blocked[0][0].get("collector_id"),
                range_ids=[r["id"] for r, _ in blocked],
                reason=f"skipped: change freeze {b['name']} until "
                       f"{b['ends_at']:%Y-%m-%d %H:%M} UTC")
            log.info("scheduled sweep skipped for a change freeze",
                     schedule=sched["name"], ranges=len(blocked), freeze=b["name"])
        if not live:
            await repo.advance_schedule(session, sched["id"], skipped_id,
                                        schedule_time.next_after(sched, datetime.now(UTC)))
            continue
        lanes = sorted({r["collector_id"] for r in live}, key=lambda c: c or "")
        if await repo.run_in_flight(session, lanes):
            continue
        runs = await discovery_ranges.queue(
            session, range_ids=[r["id"] for r in live], schedule_id=sched["id"],
            schedule_label=sched["name"], override_blackout=True)
        await repo.advance_schedule(session, sched["id"], runs[0]["id"],
                                    schedule_time.next_after(sched, datetime.now(UTC)))
        log.info("scheduled sweep queued", schedule=sched["name"],
                 runs=[r["id"] for r in runs], ranges=len(live),
                 every_h=sched["interval_hours"])
        return {"schedule": sched, "run": runs[0], "runs": runs}
    return None


#: A queued sweep gives up when nothing that may take it has checked in for this
#: long. NOT when it has merely waited this long: a collector sweeps its runs one
#: after another, so a queued run behind two /20s legitimately waits hours.
COLLECTOR_GONE_S = 30 * 60

#: A running sweep's allowance: this floor, or twice its worst case - every
#: address silent, on both protocols (SNMP then Redfish), at the collector's
#: 1.5 s per silent address - whichever is longer. A /24 gets the floor; a /20
#: gets about seven hours.
RUNNING_FLOOR_S = 30 * 60
SECONDS_PER_SILENT_PROBE = (6 * 2) / 8
PROTOCOLS_SWEPT = 2


def running_allowance_s(scope: dict[str, Any] | None) -> float:
    probes = 0
    exclude = list((scope or {}).get("exclude") or [])
    for cidr in (scope or {}).get("subnets") or []:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        mine = [e for e in exclude
                if ipaddress.ip_network(e, strict=False).subnet_of(net)]
        probes += discovery_ranges.probe_count(net, mine)
    worst = probes * SECONDS_PER_SILENT_PROBE * PROTOCOLS_SWEPT
    return max(RUNNING_FLOOR_S, 2 * worst)


def _ago(seconds: float) -> str:
    if seconds < 3600:
        return f"{round(seconds / 60)} min"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h"
    return f"{round(seconds / 86400)} days"


def stuck_reason(run: dict[str, Any]) -> str | None:
    """Why this unfinished run should be given up on, or None to keep waiting."""
    if run["status"] == "running":
        took = float(run["running_s"] or 0)
        limit = running_allowance_s(run["scope"])
        if took > limit:
            return (f"timed out: the collector took it {_ago(took)} ago and never "
                    f"reported (allowed {_ago(limit)})")
        return None
    if float(run["queued_s"] or 0) < COLLECTOR_GONE_S:
        return None
    if run["collector_id"]:
        if not run["collector_registered"]:
            return f"gave up: {run['collector_id']} has never checked in"
        age = float(run["collector_age_s"] or 0)
        if age > COLLECTOR_GONE_S:
            return f"gave up: {run['collector_id']} last checked in {_ago(age)} ago"
        return None
    freshest = run["freshest_collector_s"]
    if freshest is None or float(freshest) > COLLECTOR_GONE_S:
        return "gave up: no collector has checked in"
    return None


async def expire_stuck_runs(session: AsyncSession) -> int:
    """Fail sweeps that will never finish, and say why.

    A run queued for a collector that is gone waited for ever, and a run whose
    collector died mid-sweep stayed "running" for ever - and both held back every
    schedule for that collector. Failed rather than cancelled: nobody decided
    this, something broke, and it should read as a fault.
    """
    n = 0
    for run in await repo.unfinished_runs(session):
        reason = stuck_reason(run)
        if reason and await repo.fail_run(session, run["id"], reason):
            n += 1
            log.warning("discovery run timed out", run_id=run["id"],
                        status=run["status"], collector=run["collector_id"],
                        reason=reason)
    return n
