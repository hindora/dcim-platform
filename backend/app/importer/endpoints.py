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

# core/snmprec_generator.py::_NO_SNMP_TYPES
NO_SNMP_TYPES = frozenset({"rpp", "chiller", "pump", "cooling_tower", "valve"})

# energy_monitor is the Verdigris EV2 branch-circuit panel, which is a
# BACnet/IP device like the mechanical plant.
BACNET_TYPES = frozenset({"chiller", "pump", "cooling_tower", "valve", "crah",
                          "cdu", "energy_monitor"})

NETWORK_TYPES = frozenset({"switch", "router", "firewall", "load_balancer", "oob_switch"})

# Default poll profile per device type, by protocol.
SNMP_PROFILE_BY_TYPE = {
    "server": "snmp-server-30s",
    "switch": "snmp-network-30s",
    "router": "snmp-network-30s",
    "firewall": "snmp-network-30s",
    "load_balancer": "snmp-network-30s",
    "oob_switch": "snmp-network-30s",
    "sensor": "snmp-sensor-10s",
}
DEFAULT_SNMP_PROFILE = "snmp-power-30s"


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
        "credential_hint": f"community: {addr}",
    }


def derive_endpoints(
    dev: dict,
    *,
    include_protocols: frozenset[str] = frozenset({"snmp"}),
    gnmi_server_host: str = "127.0.0.1",
    redfish_username: str = "admin",
    redfish_password: str = "password",
    redfish_scheme: str = "http",
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
                poll_profile="snmp-bmc-60s",
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
            credential_hint=f"user: {redfish_username}",
        ))

    # ------------------------------------------------------------------ gNMI
    if "gnmi" in include_protocols and dtype in NETWORK_TYPES:
        target = dev.get("mgmt_ip") or dev.get("ip_address")
        if target:
            out.append(EndpointSpec(
                protocol="gnmi", role="native_card",
                # One gRPC server serves every target; the device is selected by
                # prefix.target, not by the destination address.
                address=gnmi_server_host,
                port=int(dev.get("gnmi_port") or 57400),
                poll_profile="gnmi-stream",
                addressing={"target": target, "insecure": True},
            ))

    # ---------------------------------------------------------------- BACnet
    if "bacnet" in include_protocols and dtype in BACNET_TYPES:
        if dev.get("mstp_router_ip"):
            out.append(EndpointSpec(
                protocol="bacnet", role="field_device",
                address=dev["mstp_router_ip"], port=47808,
                poll_profile="bacnet-10s",
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
                    poll_profile="bacnet-10s",
                    # The controller assigns the device instance; the adapter
                    # discovers it with a directed Who-Is rather than assuming
                    # a formula. Carried here only when the export knows it.
                    addressing=({"device_instance": dev["bacnet_instance"]}
                                if dev.get("bacnet_instance") else {}),
                ))

    # ---------------------------------------------------------------- Modbus
    if "modbus" in include_protocols:
        role = dev.get("modbus_role") or ""
        if role == "server":
            addr = dev.get("mgmt_ip") or dev.get("ip_address")
            if addr:
                out.append(EndpointSpec(
                    protocol="modbus", role="native_card", address=addr, port=502,
                    poll_profile="modbus-30s",
                    addressing={"unit_id": dev.get("modbus_unit_id") or 1}))
        elif role == "gateway":
            addr = dev.get("mgmt_ip") or dev.get("ip_address")
            if addr:
                out.append(EndpointSpec(
                    protocol="modbus", role="gateway", address=addr, port=502,
                    poll_profile="modbus-30s", addressing={"unit_id": 0}))
        elif role == "rtu_slave" and dev.get("modbus_gateway_ip"):
            out.append(EndpointSpec(
                protocol="modbus", role="field_device",
                address=dev["modbus_gateway_ip"], port=502,
                poll_profile="modbus-30s",
                addressing={"unit_id": dev.get("modbus_unit_id")},
                via_address=dev["modbus_gateway_ip"]))

    return out
