"""How a promoted responder will be polled: its endpoints, decided at promotion.

Promotion used to create the device and nothing else. The record landed
`installed` with no endpoint, so nothing polled it, the commissioning soak had no
telemetry to measure, and an operator had to find the device again and wire up
its monitoring by hand - re-entering a credential the sweep had just proved.

In a real onboarding flow (SolarWinds, LibreNMS, dcTrack, Device42) the credential
set that answered during discovery is carried onto the node it becomes. The same
here, with one deliberate limit: the collector reports WHICH of its configured
credentials answered, by reference, and never the secret. So this module can
recreate a credential only where the reference is enough to rebuild it - the
simulator's "the community is the address" convention - and asks a person for
everything else.

Roles and poll profiles follow the importer's rules (app/importer/endpoints.py),
not a second copy of them: the two paths into inventory must not disagree about
how a Raritan strip or a BMC is polled.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import credential_hint, encrypt_secret
from app.importer.endpoints import (
    DEFAULT_SNMP_PROFILE,
    NETWORK_PROFILE_TYPES,
    SNMP_PROFILE_BY_TYPE,
    network_profile,
    pdu_profile,
)
from app.repositories import devices as devices_repo
from app.services import credentials as credentials_svc
from app.services import endpoint_config

#: What a sweep speaks, and so what a promotion can wire up. A BACnet or Modbus
#: device reaches inventory through its gateway's import, not through a sweep.
PROMOTABLE_PROTOCOLS = ("snmp", "redfish")

SNMP_BMC_PROFILE = "snmp-bmc-120s"
REDFISH_PROFILE = "redfish-60s"


class EndpointPlanError(ValueError):
    """A monitoring choice that cannot be honoured, in the operator's words."""


def role_for(protocol: str, device_type: str, address: str | None,
             redfish_addresses: set[str]) -> str:
    """Which agent on the box answered.

    A server's SNMP at an address that ALSO answered Redfish is its BMC - the
    controller serves both - and polling it with the host-resources profile asks
    an iDRAC for hrStorage it does not have. At any other address it is the OS.
    """
    if protocol == "redfish":
        return "bmc"
    if device_type != "server":
        return "native_card"
    return "bmc" if address and address in redfish_addresses else "os_agent"


def default_profile(protocol: str, role: str, device_type: str,
                    vendor: str | None) -> str:
    """The importer's profile for this agent. Vendor matters for PDUs and network
    gear, whose MIB families share no OIDs."""
    if protocol == "redfish":
        return REDFISH_PROFILE
    if role == "bmc":
        return SNMP_BMC_PROFILE
    dev = {"device_type": device_type, "vendor": vendor or ""}
    if device_type == "pdu":
        return pdu_profile(dev)
    if device_type in NETWORK_PROFILE_TYPES:
        return network_profile(dev)
    return SNMP_PROFILE_BY_TYPE.get(device_type, DEFAULT_SNMP_PROFILE)


def _access(cand: dict[str, Any]) -> dict[str, Any]:
    return dict((cand.get("identity") or {}).get("access") or {})


def _port(cand: dict[str, Any]) -> int:
    raw = _access(cand).get("port")
    try:
        return int(raw) if raw else endpoint_config.DEFAULT_PORT[cand["protocol"]]
    except (TypeError, ValueError):
        return endpoint_config.DEFAULT_PORT[cand["protocol"]]


def suggested_credential(cand: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """What the sweep's evidence lets us propose, and why - or None and why not."""
    access = _access(cand)
    if cand["protocol"] == "snmp":
        if access.get("version") == "3":
            # docs/26 Phase 4: a pool's SNMPv3 credential answered. The endpoint
            # inherits it rather than getting a copy, so a rotation of the
            # pool's credential reaches it - and the sweep's evidence is about
            # exactly that credential, by reference.
            if access.get("credential") == "pool" and access.get("pool_id"):
                return ({"mode": "pool", "pool_id": access["pool_id"]},
                        "the sweep authenticated with its pool's SNMPv3 credential")
            return (None, "speaks SNMPv3 (engine "
                          f"{access.get('engine_id') or '?'}) but no credential this "
                          "collector holds was accepted; set the pool's SNMPv3 "
                          "credential, or choose one")
        also = (" It also speaks SNMPv3: once its pool has an SNMPv3 credential, a "
                "fresh sweep can move it off v2c.") if access.get("v3_engine_id") else ""
        if access.get("community") == "address":
            return ({"mode": "address"},
                    "the sweep was answered with this address as the community." + also)
        if access.get("community") == "configured":
            return (None, f"answered the collector's configured community "
                          f"#{access.get('community_index', '?')}; the value stays on "
                          f"the collector, so choose the matching credential." + also)
        return None, "this sweep did not record how it authenticated"
    if access.get("credential") == "configured":
        return (None, f"the collector's configured login #"
                      f"{access.get('credential_index', '?')} read the chassis; the "
                      f"password stays on the collector, so choose the matching "
                      f"credential")
    if access.get("credential") == "none":
        return None, "no login this collector holds opens this BMC"
    return None, "this sweep did not record how it authenticated"


async def machine_probes(session: AsyncSession,
                         cand: dict[str, Any]) -> list[dict[str, Any]]:
    """Every probe that reached the same machine as `cand`: same address, or the
    same serial at another address (a BMC and its production NIC).

    Open or just-promoted only. A dismissed probe was a decision about that
    protocol, and wiring it up here would quietly reverse it.
    """
    rows = (await session.execute(text("""
        SELECT id::text, host(address) AS address, protocol::text AS protocol,
               identity, serial, status
          FROM discovery_candidate
         WHERE status IN ('new', 'promoted')
           AND protocol::text = ANY(:protocols)
           AND (id = CAST(:id AS uuid)
                OR (address IS NOT NULL AND address = CAST(:addr AS inet))
                OR (serial IS NOT NULL AND serial = :serial))
         ORDER BY protocol, address
    """), {"id": cand["id"], "addr": cand.get("address"),
           "serial": (cand.get("identity") or {}).get("serial") or cand.get("serial"),
           "protocols": list(PROMOTABLE_PROTOCOLS)})).mappings().all()
    return [dict(r) for r in rows]


async def plan(session: AsyncSession, cand: dict[str, Any], *,
               device_type: str, vendor: str | None) -> list[dict[str, Any]]:
    """The endpoints promotion would create, each with its defaults and whatever
    credential the evidence supports. Read-only: the dialog shows this, and the
    operator's answer is what `create` receives."""
    probes = await machine_probes(session, cand)
    redfish_at = {p["address"] for p in probes if p["protocol"] == "redfish"}
    out = []
    for p in probes:
        role = role_for(p["protocol"], device_type, p["address"], redfish_at)
        cred, why = suggested_credential(p)
        out.append({
            "candidate_id": p["id"], "protocol": p["protocol"], "address": p["address"],
            "port": _port(p), "role": role,
            "poll_profile": default_profile(p["protocol"], role, device_type, vendor),
            "scheme": _access(p).get("scheme"),
            "suggested_credential": cred, "credential_note": why,
        })
    return out


async def _resolved_pool(session: AsyncSession, device_id: str,
                         address: str) -> str | None:
    """The pool an endpoint at `address` on this device will resolve into: the
    most specific discovery range holding the address, in the device's
    datacenter - repositories/pools._RESOLVED_POOL's rule, for an endpoint
    that does not exist yet."""
    return (await session.execute(text("""
        SELECT cp.id::text
          FROM device d
          LEFT JOIN rack rk     ON rk.id = d.rack_id
          LEFT JOIN rack_row rr ON rr.id = rk.row_id
          LEFT JOIN room rm     ON rm.id = COALESCE(rr.room_id, d.room_id)
          JOIN discovery_range dr ON CAST(:addr AS inet) <<= dr.cidr
          JOIN collector_pool cp
            ON cp.datacenter_id = rm.datacenter_id AND cp.plane = dr.purpose
         WHERE d.id = CAST(:dev AS uuid)
         ORDER BY masklen(dr.cidr) DESC
         LIMIT 1
    """), {"dev": device_id, "addr": address})).scalar_one_or_none()


async def _credential(session: AsyncSession, protocol: str, address: str,
                      choice: dict[str, Any], device_id: str) -> str | None:
    """Resolve the operator's credential choice to a credential id - or None
    for "inherit the pool's default"."""
    mode = choice.get("mode")
    if mode == "pool":
        if protocol != "snmp":
            raise EndpointPlanError("only an SNMP endpoint inherits a pool credential")
        pool = await _resolved_pool(session, device_id, address)
        if pool is None:
            raise EndpointPlanError(
                "this address is in no pool's range here, so there is no pool "
                "credential to inherit; choose a credential")
        want = str(choice.get("pool_id") or "")
        if want and pool != want:
            # The sweep proved ONE pool's credential. Inheriting another pool's
            # would be a guess dressed up as evidence.
            raise EndpointPlanError(
                "this address resolves to a different pool from the one whose "
                "credential answered the sweep; choose a credential")
        if "snmp" not in await credentials_svc.pool_defaults(session, pool):
            raise EndpointPlanError("the pool has no SNMP default credential to inherit")
        return None
    if mode == "existing":
        cid = str(choice.get("id") or "").strip()
        if not cid:
            raise EndpointPlanError("choose which credential to use")
        try:
            cred = await devices_repo.get_credential(session, cid)
        except Exception:  # not a uuid: the cast fails before any row is read
            cred = None
        if cred is None:
            raise EndpointPlanError("that credential no longer exists")
        try:
            endpoint_config.check_credential(protocol, cred)
        except endpoint_config.EndpointConfigError as exc:
            raise EndpointPlanError(str(exc)) from None
        return cred["id"]

    if mode == "address":
        if protocol != "snmp":
            raise EndpointPlanError("only an SNMP community can be the address")
        kind, payload = "snmp_v2c", {"community": address}
        name = f"snmp-v2c-{address}"
        # The importer's name for the same derivation, so a device promoted here
        # and one imported share one row rather than two identical secrets. Reused
        # as it stands: somebody may since have rotated it, and a promotion is not
        # the place to undo that.
        existing = await _credential_by_name(session, name)
        if existing:
            return existing
    elif mode == "new":
        if protocol == "snmp":
            community = str(choice.get("community") or "")
            if not community.strip():
                raise EndpointPlanError("a new SNMP credential needs its community")
            kind, payload = "snmp_v2c", {"community": community}
            name = f"snmp-v2c-{address}"
        else:
            user = str(choice.get("username") or "").strip()
            password = str(choice.get("password") or "")
            if not user or not password:
                raise EndpointPlanError("a new Redfish login needs a username and "
                                        "a password")
            kind, payload = "http_basic", {"username": user, "password": password}
            name = f"redfish-{address}"
        # Never overwrite. A credential with this name is polling something
        # already, and replacing its secret from a promote dialog would break
        # that endpoint silently.
        if await _credential_by_name(session, name):
            raise EndpointPlanError(
                f"a credential named {name} already exists; choose it under "
                f"Existing rather than replacing it")
    else:
        raise EndpointPlanError(f"unknown credential choice {mode!r}")

    # docs/26 Phase 4: a newly-promoted credential is sealed under whatever
    # key rotation currently calls active, not always the original
    # DCIM_CREDENTIAL_KEY - key_id is stored alongside the encrypted blob so
    # a later read knows which one to reach for.
    active_key_id = get_settings().active_credential_key_id
    return (await session.execute(text("""
        INSERT INTO credential (name, protocol, kind, secret_enc, secret_hint, key_id)
        VALUES (:name, CAST(:proto AS protocol_t), :kind, :blob, :hint, :key_id)
        RETURNING id::text
    """), {"name": name, "proto": protocol, "kind": kind,
           "blob": encrypt_secret(payload, key_id=active_key_id),
           "hint": credential_hint(kind, payload),
           "key_id": active_key_id})).scalar_one()


def _requested_port(req: dict[str, Any], probe: dict[str, Any]) -> int:
    """The operator's port, checked, else the one the sweep reached it on."""
    if req.get("port") is None:
        return _port(probe)
    try:
        return int(endpoint_config.validate_port(int(req["port"])) or _port(probe))
    except (TypeError, ValueError) as exc:
        raise EndpointPlanError(str(exc) or "port must be a number") from None


async def settle_probes(session: AsyncSession, cand: dict[str, Any],
                        device_id: str) -> int:
    """Mark every probe of the promoted machine as promoted, and matched.

    Promotion used to mark only the probe it was called on. A server answers
    Redfish AND SNMP on its BMC; promoting the Redfish half left the SNMP half
    `new` and unmatched, so the page grouped the two, saw an open probe with no
    record, and went on calling the machine "not in inventory" until the next
    sweep matched it by address.
    """
    # The promoted probe itself too: set_candidate_status moved it to `promoted`
    # without saying WHICH record it became.
    ids = [p["id"] for p in await machine_probes(session, cand)
           if p["status"] == "new" or p["id"] == cand["id"]]
    if not ids:
        return 0
    await session.execute(text("""
        UPDATE discovery_candidate
           SET status = 'promoted', matched_device_id = CAST(:dev AS uuid)
         WHERE id = ANY(CAST(:ids AS uuid[]))
    """), {"dev": device_id, "ids": ids})
    return len(ids)


async def _credential_by_name(session: AsyncSession, name: str) -> str | None:
    return (await session.execute(text(
        "SELECT id::text FROM credential WHERE name = :n"), {"n": name})).scalar()


async def create(session: AsyncSession, *, device_id: str, device_type: str,
                 vendor: str | None, cand: dict[str, Any],
                 requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create the endpoints the operator chose. All or nothing - the caller's
    transaction - so a bad credential choice on the second endpoint does not leave
    a device polled on half its agents."""
    probes = {p["id"]: p for p in await machine_probes(session, cand)}
    redfish_at = {p["address"] for p in probes.values() if p["protocol"] == "redfish"}
    made: list[dict[str, Any]] = []
    for req in requests:
        p = probes.get(str(req.get("candidate_id") or ""))
        if p is None:
            # Only the machine being promoted. Otherwise a promote body could wire
            # an endpoint to any responder on the network.
            raise EndpointPlanError("that probe is not part of the machine being "
                                    "promoted")
        protocol = p["protocol"]
        role = role_for(protocol, device_type, p["address"], redfish_at)
        profile_name = default_profile(protocol, role, device_type, vendor)
        profile_id = req.get("poll_profile_id")
        if profile_id:
            if await devices_repo.get_poll_profile(session, profile_id) is None:
                raise EndpointPlanError("no such poll profile")
        else:
            found = await devices_repo.get_poll_profile_by_name(session, profile_name)
            if found is None:
                raise EndpointPlanError(f"poll profile {profile_name} is missing")
            profile_id = found["id"]
        cred_id = await _credential(session, protocol, p["address"],
                                    req.get("credential") or {}, device_id)
        addressing: dict[str, Any] = {}
        if protocol == "redfish":
            # The scheme the sweep actually reached it on. A real BMC is https;
            # the simulator serves plain http on 8443, and the adapter never
            # downgrades on its own, so the scheme has to be what answered.
            # verify_tls off: a factory BMC certificate is self-signed, as the
            # importer assumes too. A site with a BMC CA turns it on per endpoint.
            addressing = {"base": "/redfish/v1",
                          "scheme": _access(p).get("scheme") or "https",
                          "verify_tls": False}
        endpoint_id = (await session.execute(text("""
            INSERT INTO device_endpoint (device_id, protocol, role, address, port,
                                         addressing, credential_id, poll_profile_id,
                                         collector_id, enabled)
            VALUES (CAST(:dev AS uuid), CAST(:proto AS protocol_t),
                    CAST(:role AS endpoint_role_t), CAST(:addr AS inet), :port,
                    CAST(:addressing AS jsonb), CAST(:cred AS uuid),
                    CAST(:profile AS uuid), NULL, true)
            ON CONFLICT (device_id, protocol, role, coalesce(host(address), ''))
            DO UPDATE SET port = EXCLUDED.port, addressing = EXCLUDED.addressing,
                          credential_id = EXCLUDED.credential_id,
                          poll_profile_id = EXCLUDED.poll_profile_id,
                          enabled = true, updated_at = now()
            RETURNING id::text
        """), {"dev": device_id, "proto": protocol, "role": role,
               "addr": p["address"], "port": _requested_port(req, p),
               "addressing": json.dumps(addressing), "cred": cred_id,
               "profile": profile_id})).scalar_one()
        made.append({"endpoint_id": endpoint_id, "protocol": protocol,
                     "address": p["address"], "role": role,
                     "poll_profile": profile_name if not req.get("poll_profile_id")
                     else None})
    return made
