"""Endpoint derivation from a simulator device record.

Every rule here was read out of the simulator's source, and the reason is given
because these are exactly the rules that look arbitrary until they bite:

* ``snmp_community == the device's SNMP IP address``, never "public"
  (``Device.__post_init__`` in core/device_manager.py). A wrong community is a
  SILENT DROP - snmpsim finds no dataset and does not answer - which looks
  identical to a dead device.

* Which IP answers SNMP depends on the device type
  (``SNMPRecGenerator.snmp_address``): a SERVER's OS agent binds the production
  NIC, while everything else answers on the OOB management IP. A server's
  management IP belongs to its BMC, which is a SECOND, SEPARATE agent with its
  own MIB subtree.

* Five device types carry no SNMP agent at all (``_NO_SNMP_TYPES``): RPP,
  chiller, pump, cooling tower, valve. That is realistic - real chillers and
  pumps have no SNMP card, the BMS gateways their points - so creating SNMP
  endpoints for them would produce permanent false "unreachable" alarms.

* BACnet MS/TP devices and Modbus RTU slaves have NO IP of their own. They are
  reached through a router/gateway, which is what ``via`` expresses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.security import credential_hint

# core/snmprec_generator.py::_NO_SNMP_TYPES
NO_SNMP_TYPES = frozenset({"rpp", "chiller", "pump", "cooling_tower", "valve"})

# energy_monitor is the Verdigris EV2 branch-circuit panel, which is a
# BACnet/IP device like the mechanical plant.
BACNET_TYPES = frozenset({"chiller", "pump", "cooling_tower", "valve", "crah",
                          "cdu", "energy_monitor"})

NETWORK_TYPES = frozenset({"switch", "router", "firewall", "load_balancer", "oob_switch"})

# Device types that speak gNMI. Narrower than NETWORK_TYPES on purpose: gNMI is
# a fabric feature. Switches and routers from Arista, Cisco, Juniper and Nokia
# ship it; firewalls and load balancers expose vendor APIs instead, and console
# or OOB switches usually speak SNMP and nothing else. The plane agrees - it
# serves 46 targets, every one a switch or a router - and creating endpoints
# for the other 52 produced 52 permanently reconnecting sessions whose only
# message was that nothing was listening.
GNMI_TYPES = frozenset({"switch", "router"})

# Device types with a native Modbus/TCP server. Mirrors MODBUS_MAPS in
# core/modbus_register_map.py. CRAH, CDU, PDU and RPP are deliberately absent:
# their cards really do speak Modbus, but the same values already arrive over
# SNMP and BACnet, and a third rendering of identical numbers is maintenance
# without signal.
MODBUS_NATIVE_TYPES = frozenset({
    "utility_feed", "switchgear", "mcc", "mpp", "generator", "ats", "ups",
})

# Plant header probes, by the prefix of their name. Mirrors _PROBE_ROLES in
# core/device_state_store.py, which derives the same role the same way - the
# instrument's identity is in its tag, exactly as it is on a real drawing.
PROBE_ROLE_BY_PREFIX = {
    "CHWS": "chw_supply",
    "CHWR": "chw_return",
    "CWS": "cw_supply",
    "CWR": "cw_return",
    "CTB": "ct_basin",
    "FLOW": "chw_flow",
}


def _probe_role(dev: dict) -> str | None:
    """The plant header point a transmitter measures, from its tag."""
    name = str(dev.get("name") or "")
    return PROBE_ROLE_BY_PREFIX.get(name.split("-")[0].upper())

# Default poll profile per device type, by protocol.
SNMP_PROFILE_BY_TYPE = {
    "server": "snmp-server-120s",
    "switch": "snmp-network-600s",
    "router": "snmp-network-600s",
    "firewall": "snmp-network-600s",
    "load_balancer": "snmp-network-600s",
    "oob_switch": "snmp-network-600s",
    "sensor": "snmp-sensor-10s",
}
DEFAULT_SNMP_PROFILE = "snmp-power-120s"


@dataclass(slots=True)
class EndpointSpec:
    protocol: str
    role: str
    address: str | None
    port: int | None
    poll_profile: str
    addressing: dict[str, Any] = field(default_factory=dict)
    credential_kind: str | None = None
    credential_payload: dict[str, Any] | None = None
    credential_name: str | None = None
    credential_hint: str | None = None
    # Address of the gateway/router this endpoint is reached through, resolved
    # to a via_endpoint_id once every endpoint row exists.
    via_address: str | None = None
    enabled: bool = True


def snmp_address(dev: dict) -> str | None:
    """core/snmprec_generator.py::snmp_address"""
    if dev.get("device_type") == "server":
        return dev.get("ip_address") or dev.get("mgmt_ip") or None
    return dev.get("mgmt_ip") or dev.get("ip_address") or None


def bmc_address(dev: dict) -> str | None:
    """core/snmprec_generator.py::bmc_address - '' for anything but a server."""
    if dev.get("device_type") == "server" and dev.get("mgmt_ip"):
        return dev["mgmt_ip"]
    return None


def _snmp_credential(addr: str) -> dict[str, Any]:
    # The community IS the address. Stored encrypted like any other secret even
    # though it is derivable, because the DCIM must not special-case "weak"
    # credentials - real deployments put a real community here.
    return {
        "credential_kind": "snmp_v2c",
        "credential_payload": {"community": addr},
        "credential_name": f"snmp-v2c-{addr}",
        # Never "community: {addr}": on this fleet the community IS the
        # address, so that hint is the credential written out in plaintext -
        # stored unencrypted and served to every reader by GET /devices.
        "credential_hint": credential_hint("snmp_v2c", {"community": addr}),
    }


def derive_endpoints(
    dev: dict,
    *,
    include_protocols: frozenset[str] = frozenset({"snmp"}),
    gnmi_gateway: str | None = None,
    redfish_username: str = "admin",
    redfish_password: str = "password",
    redfish_scheme: str = "http",
    gnmi_port: int = 50051,
) -> list[EndpointSpec]:
    """Return the protocol endpoints implied by one simulator device record.

    ``include_protocols`` gates which adapters exist yet. Phase 1 ships the SNMP
    adapter only; widening the set is the entire change needed when the others
    land.
    """
    dtype = dev.get("device_type") or ""
    out: list[EndpointSpec] = []

    # ------------------------------------------------------------------ SNMP
    if "snmp" in include_protocols and dtype not in NO_SNMP_TYPES:
        addr = snmp_address(dev)
        if addr:
            out.append(EndpointSpec(
                protocol="snmp",
                role="os_agent" if dtype == "server" else "native_card",
                address=addr,
                port=int(dev.get("snmp_port") or 161),
                poll_profile=SNMP_PROFILE_BY_TYPE.get(dtype, DEFAULT_SNMP_PROFILE),
                **_snmp_credential(addr),
            ))
        bmc = bmc_address(dev)
        # Only when it is genuinely a different agent from the OS one.
        if bmc and bmc != addr:
            out.append(EndpointSpec(
                protocol="snmp", role="bmc", address=bmc, port=161,
                poll_profile="snmp-bmc-120s",
                **_snmp_credential(bmc),
            ))

    # --------------------------------------------------------------- Redfish
    if "redfish" in include_protocols and dtype == "server" and dev.get("mgmt_ip"):
        out.append(EndpointSpec(
            protocol="redfish", role="bmc", address=dev["mgmt_ip"], port=8443,
            poll_profile="redfish-60s",
            # The simulator serves Redfish as PLAIN HTTP on 8443, despite the
            # port. Real BMCs speak TLS, so the adapter defaults to https and
            # never downgrades on its own - a silent fallback would put the BMC
            # password on the wire in clear. Declaring the scheme here keeps
            # pointing at real hardware a data change rather than a code one.
            addressing={"base": "/redfish/v1", "scheme": redfish_scheme,
                        "verify_tls": False},
            credential_kind="http_basic",
            credential_payload={"username": redfish_username, "password": redfish_password},
            credential_name=f"redfish-{dev['mgmt_ip']}",
            credential_hint=credential_hint(
                "http_basic", {"username": redfish_username,
                               "password": redfish_password}),
        ))

    # ------------------------------------------------------------------ gNMI
    if "gnmi" in include_protocols and dtype in GNMI_TYPES:
        target = dev.get("mgmt_ip") or dev.get("ip_address")
        if target:
            out.append(EndpointSpec(
                protocol="gnmi", role="native_card",
                # Each device serves gNMI on ITS OWN address, which is how real
                # gear works and what this plane actually does - 46 listeners on
                # 46 device IPs, verified with ss. An earlier version dialled a
                # single shared server and selected the device with
                # prefix.target; that shape exists (a collector-facing gNMI
                # gateway) but is not what is in front of us, and every endpoint
                # would have pointed at a port nothing was listening on.
                # A deployment that fronts its devices with a shared gNMI
                # gateway dials the gateway and selects the device with
                # prefix.target. That shape is real and supported; it is just
                # not what this plane does.
                address=(gnmi_gateway or target),
                # The per-device gnmi_port in the export is NOT what the
                # controller binds: every device carries 57400 while the
                # listeners are on 50051 (verified with ss against the running
                # plane). The port the server actually listens on is the
                # authoritative one, so it comes from configuration here
                # rather than from a device field that nothing updates.
                port=gnmi_port,
                poll_profile="gnmi-stream",
                # The target is still sent: gear that fronts several devices
                # needs it, and gear that serves only itself ignores it.
                addressing={"target": target, "insecure": True},
            ))

    # ---------------------------------------------------------------- BACnet
    if "bacnet" in include_protocols and dtype in BACNET_TYPES:
        if dev.get("mstp_router_ip"):
            out.append(EndpointSpec(
                protocol="bacnet", role="field_device",
                address=dev["mstp_router_ip"], port=47808,
                poll_profile="bacnet-30s",
                # No device_instance: the controller assigns it in
                # commissioning order, so the adapter asks with a directed
                # Who-Is rather than assuming a formula. A device on a trunk
                # has no IP of its own, so (network, mac) is its address.
                addressing={"network": dev.get("mstp_net"),
                            "mac": dev.get("mstp_mac")},
                via_address=dev["mstp_router_ip"],
            ))
        else:
            addr = dev.get("mgmt_ip") or dev.get("ip_address")
            if addr:
                out.append(EndpointSpec(
                    protocol="bacnet", role="native_card", address=addr, port=47808,
                    poll_profile="bacnet-30s",
                    # The controller assigns the device instance; the adapter
                    # discovers it with a directed Who-Is rather than assuming
                    # a formula. Carried here only when the export knows it.
                    addressing=({"device_instance": dev["bacnet_instance"]}
                                if dev.get("bacnet_instance") else {}),
                ))

    # ---------------------------------------------------------------- Modbus
    if "modbus" in include_protocols:
        role = dev.get("modbus_role") or ""
        if role == "rtu_slave" and dev.get("modbus_gateway_ip"):
            # A field instrument on an RS-485 trunk owns no address: the
            # gateway IP carries the request and the unit id says which
            # device on the trunk answers.
            addressing: dict[str, Any] = {"unit_id": dev.get("modbus_unit_id")}
            probe = _probe_role(dev)
            if probe:
                # A transmitter publishes one nameless process value; what it
                # MEANS comes from where it is installed. Without this the
                # adapter cannot even tell an RTD from a flow meter, since
                # both are device_type "sensor".
                addressing["probe_role"] = probe
            out.append(EndpointSpec(
                protocol="modbus", role="field_device",
                address=dev["modbus_gateway_ip"], port=502,
                poll_profile="modbus-30s",
                addressing=addressing,
                via_address=dev["modbus_gateway_ip"]))
        elif role == "gateway":
            # The gateway itself is not a meter. It is recorded so the RTU
            # slaves have something to hang `via_endpoint_id` from, and so an
            # unreachable trunk is attributable to the box in front of it.
            addr = dev.get("mgmt_ip") or dev.get("ip_address")
            if addr:
                out.append(EndpointSpec(
                    protocol="modbus", role="gateway", address=addr, port=502,
                    poll_profile="modbus-30s", addressing={"unit_id": 0},
                    enabled=False))
        elif dtype in MODBUS_NATIVE_TYPES:
            # Electrical gear speaks Modbus/TCP directly. The export carries no
            # modbus_role for these - a role is only set for the serial trunk -
            # so keying on the role alone created endpoints for the twelve
            # transmitters and none of the thirty meters, switchgear, gensets,
            # transfer switches and UPS that carry the site's electrical
            # telemetry.
            addr = dev.get("mgmt_ip") or dev.get("ip_address")
            if addr:
                out.append(EndpointSpec(
                    protocol="modbus", role="native_card", address=addr, port=502,
                    poll_profile="modbus-30s",
                    addressing={"unit_id": dev.get("modbus_unit_id") or 1}))

    return out
