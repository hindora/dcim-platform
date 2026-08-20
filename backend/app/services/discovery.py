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
    (r"\brouter\b", "router"),
    (r"\bfirewall\b", "firewall"),
    (r"load balanc", "load_balancer"),
    (r"\bswitch\b|\bios\b|nx-os|junos|arista", "switch"),
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
]


def classify(identity: dict[str, Any]) -> tuple[str | None, str | None]:
    """Guess a device type and vendor from what the probe could read."""
    blob = " ".join(str(v) for v in identity.values() if v).lower()
    if not blob:
        return None, None
    dtype = next((t for pattern, t in _TYPE_HINTS if re.search(pattern, blob)), None)
    vendor = next((v for pattern, v in _VENDOR_HINTS if re.search(pattern, blob)), None)
    return dtype, vendor


async def create_run(session: AsyncSession, *, method: str,
                     subnets: list[str]) -> dict[str, Any]:
    if method != "snmp_sweep":
        raise DiscoveryError(
            f"method {method!r} is not implemented; only 'snmp_sweep' is")
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


async def record_results(session: AsyncSession, run_id: str,
                         responders: list[dict[str, Any]]) -> dict[str, int]:
    """Stage what a sweep found and mark which of it inventory already knows."""
    addresses = [r["address"] for r in responders if r.get("address")]
    known = await repo.match_addresses(session, addresses)

    unmanaged = 0
    for r in responders:
        addr = r.get("address")
        if not addr:
            continue
        identity = r.get("identity") or {}
        match = known.get(addr)
        dtype, vendor = classify(identity)
        await repo.upsert_candidate(
            session, run_id=run_id, address=addr,
            protocol=r.get("protocol") or "snmp", identity=identity,
            matched_device_id=match["device_id"] if match else None,
            suggested_device_type=dtype, suggested_vendor=vendor)
        if not match:
            unmanaged += 1

    await repo.finish_run(session, run_id, found=len(responders))
    log.info("discovery run recorded", run_id=run_id, responders=len(responders),
             known=len(responders) - unmanaged, unmanaged=unmanaged)
    return {"found": len(responders), "known": len(responders) - unmanaged,
            "unmanaged": unmanaged}


async def promote(session: AsyncSession, candidate_id: str,
                  payload: dict[str, Any]) -> dict[str, Any]:
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

    row = (await session.execute(text("""
        INSERT INTO device (name, device_type, mgmt_ip, lifecycle, attributes)
        VALUES (:name, :dtype, CAST(:ip AS inet), 'in_service',
                CAST(:attrs AS jsonb))
        RETURNING id::text, name
    """), {
        "name": name, "dtype": device_type, "ip": cand["address"],
        # Keep the evidence. Six months from now the question "why is this
        # device recorded as a switch" has an answer.
        "attrs": json.dumps({
            "discovered": True,
            "discovery_candidate_id": candidate_id,
            "discovery_identity": cand["identity"],
        }),
    })).mappings().first()

    await repo.set_candidate_status(session, candidate_id, "promoted")
    log.info("candidate promoted", candidate_id=candidate_id,
             device_id=row["id"], name=row["name"])
    return {"device_id": row["id"], "name": row["name"]}


async def ignore(session: AsyncSession, candidate_id: str) -> dict[str, Any]:
    row = await repo.set_candidate_status(session, candidate_id, "ignored")
    if row is None:
        raise DiscoveryError(f"no candidate {candidate_id}")
    return row
