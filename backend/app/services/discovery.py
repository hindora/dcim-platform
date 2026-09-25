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

import json
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.repositories import discovery as repo

log = get_logger("discovery")


class DiscoveryError(ValueError):
    """Bad request, with a message meant for the caller."""


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
     r"|nx-os|junos|arista", "switch"),
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
    return dtype, vendor


#: What a run may be called. One sweep now covers every protocol the collector
#: has been configured for, so the protocol-specific name is a legacy alias.
_SWEEP_METHODS = frozenset({"sweep", "snmp_sweep"})


async def create_run(session: AsyncSession, *, method: str,
                     subnets: list[str]) -> dict[str, Any]:
    # A run sweeps whatever the collector has configured - SNMP always, Redfish
    # too where it is enabled - so `sweep` is the honest name. `snmp_sweep` is kept
    # because runs recorded under it are in the history and a queued one may still
    # be in flight; refusing it would break a name that is only misleading.
    if method not in _SWEEP_METHODS:
        raise DiscoveryError(
            f"method {method!r} is not implemented; "
            f"try one of {', '.join(sorted(_SWEEP_METHODS))}")
    if not subnets:
        raise DiscoveryError("a run needs at least one subnet to sweep")
    for net in subnets:
        # Validated here so a typo fails at request time rather than silently
        # sweeping nothing an hour later on a collector.
        if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+/\d+", net):
            raise DiscoveryError(f"{net!r} is not an IPv4 CIDR")
    run = await repo.create_run(session, method=method, scope={"subnets": subnets})
    log.info("discovery run queued", run_id=run["id"], subnets=subnets)
    return run


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


async def record_results(session: AsyncSession, run_id: str,
                         responders: list[dict[str, Any]]) -> dict[str, int]:
    """Stage what a sweep found and mark which of it inventory already knows."""
    addresses = [r["address"] for r in responders if r.get("address")]
    known = await repo.match_addresses(session, addresses)
    # The serial travels inside `identity`, which is already a free-form blob on
    # the wire, so reading it needs no change to the collector contract.
    by_serial = await repo.match_serials(
        session, [_serial_of(r) for r in responders])

    unmanaged, moved = 0, 0
    for r in responders:
        addr = r.get("address")
        if not addr:
            continue
        identity = r.get("identity") or {}
        serial = _serial_of(r)
        # SERIAL FIRST. It is the only key that survives a device being
        # re-addressed, and address matching alone reported a moved machine as
        # brand new - so promoting it created a second record for one box.
        match = by_serial.get(serial) if serial else None
        if match:
            if match.get("known_address") and match["known_address"] != addr:
                moved += 1
        else:
            match = known.get(addr)
        dtype, vendor = classify(identity, r.get("protocol") or "snmp")
        await repo.upsert_candidate(
            session, run_id=run_id, address=addr,
            protocol=r.get("protocol") or "snmp", identity=identity,
            matched_device_id=match["device_id"] if match else None,
            serial=serial,
            suggested_device_type=dtype, suggested_vendor=vendor)
        if not match:
            unmanaged += 1

    # AFTER the upserts, so anything this run saw has had its last_seen advanced and
    # only the genuinely silent addresses are left behind. A sweep is the only thing
    # that can tell "asked and got nothing" from "never asked", and it can only say
    # so about the subnets it actually covered.
    gone = await repo.mark_gone(session, run_id)

    await repo.finish_run(session, run_id, found=len(responders))
    log.info("discovery run recorded", run_id=run_id, responders=len(responders),
             known=len(responders) - unmanaged, unmanaged=unmanaged, gone=gone,
             # Worth its own number: a device matched by serial at an address
             # inventory did not expect has MOVED, and nobody recorded it.
             readdressed=moved)
    return {"found": len(responders), "known": len(responders) - unmanaged,
            "unmanaged": unmanaged, "readdressed": moved, "gone": gone}


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
        return await _attach(session, cand, candidate_id, attach_to,
                             vendor_id=vendor_id, model_id=model_id,
                             serial=serial, actor=actor)

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
    return {"device_id": row["id"], "name": row["name"]}


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
    return {"device_id": device_id, "name": target["name"], "attached": True}


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
