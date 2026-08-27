"""What may be changed about a protocol endpoint, and what may not.

Every rule here is a property of the protocol rather than of this codebase, so
each one carries the reason. The rules exist because an endpoint is the only
place where a wrong number is invisible: a bad unit ID does not fail to save,
it fails to answer, three minutes later, as an unreachable alarm on a device
that is perfectly healthy.

What this deliberately does NOT cover is the collector's own listeners - the
trap port, the BACnet local port, the Redfish event advertise address. Those
belong to the collector process, not to any device, and changing one is a
contract with the whole device plane rather than with one endpoint. They are a
separate job with a separate confirmation.
"""

from __future__ import annotations

from typing import Any

#: Where each protocol answers when nothing overrides it.
#:
#: Serving these as defaults rather than writing them into every row keeps a
#: fleet-wide correction possible: a port stored explicitly on 1,400 endpoints
#: has to be migrated, one left null follows the default.
DEFAULT_PORT = {
    "snmp": 161,
    "snmp_trap": 162,
    "gnmi": 57400,   # vendor-common; IANA registers 9339 and few use it
    "bacnet": 47808,  # 0xBAC0
    "redfish": 443,
    "modbus": 502,
    "sflow": 6343,
}

#: Protocol-specific addressing, beyond host and port.
#:
#: These are the fields that decide WHICH device answers when the address alone
#: does not - the case for everything behind a gateway - and how to talk to it
#: once it does.
#:
#: Every entry here is read by the collector. That is the whole selection rule:
#: a field the collector ignores would be a setting an operator can change with
#: no effect, which is worse than one that is missing. Checked against
#: adapters/{bacnet,modbus,gnmi,redfish} rather than invented.
ADDRESSING_FIELDS: dict[str, dict[str, dict[str, Any]]] = {
    "modbus": {
        "unit_id": {
            "label": "Unit ID",
            "min": 0, "max": 247,
            # 248-255 are reserved by the specification. 0 is the broadcast
            # address and therefore not a device - the collector treats it as
            # "unset" rather than trusting it, because gear that ignores the
            # unit id answers a 0 anyway and would look correctly addressed.
            "help": "RS-485 slave address behind the gateway, 1-247. "
                    "0 means unset; 248-255 are reserved.",
        },
        "probe_role": {
            "label": "Probe role",
            "kind": "text",
            "help": "Which register map to use for a plant instrument behind a "
                    "gateway, e.g. chw_flow.",
        },
    },
    "bacnet": {
        "network": {
            "label": "Network number",
            "min": 0, "max": 65534,
            "help": "0 for a device on this IP network. Non-zero only behind a "
                    "BACnet router, typically an MS/TP trunk.",
        },
        "mac": {
            "label": "MS/TP MAC",
            "min": 0, "max": 254,
            # 255 is the broadcast MAC and can never identify a device.
            "help": "Station address on the MS/TP trunk, 0-254. Only meaningful "
                    "with a network number.",
        },
        "device_instance": {
            "label": "Device instance",
            "min": 0, "max": 4194302,
            # 4194303 is the 'unconfigured' wildcard Who-Is uses.
            "help": "BACnet device object instance, unique across the whole "
                    "internetwork. 0 when the inventory does not know it.",
        },
    },
    "snmp": {
        "context": {
            "label": "v3 context",
            "kind": "text",
            "help": "SNMPv3 context name. Empty for the default context, which "
                    "is what nearly all gear uses.",
        },
    },
    "gnmi": {
        "target": {
            "label": "Target",
            "kind": "text",
            "help": "gNMI target name in the path prefix. Empty unless the "
                    "device fronts several targets.",
        },
        "tls": {
            "label": "TLS",
            "kind": "bool",
            "help": "Off only on a device that genuinely serves plaintext gRPC.",
        },
        "insecure": {
            "label": "Skip certificate check",
            "kind": "bool",
            "help": "Accepts the device's certificate without verifying it. "
                    "Common on gear with a self-signed default certificate, "
                    "and a real exposure on a shared management network.",
        },
    },
    "redfish": {
        "scheme": {
            "label": "Scheme",
            "kind": "choice", "choices": ["https", "http"],
            "help": "https on any real BMC. http exists for simulators and for "
                    "gear behind a terminating proxy.",
        },
        "base": {
            "label": "Service root",
            "kind": "text",
            "help": "/redfish/v1 unless the BMC publishes its service root "
                    "somewhere else.",
        },
        "verify_tls": {
            "label": "Verify certificate",
            "kind": "bool",
            "help": "Off for the self-signed certificate most BMCs ship with. "
                    "On once they carry one your CA signed.",
        },
    },
}


class EndpointConfigError(ValueError):
    """A rejected edit, with a message written for the operator making it."""


def _int_field(value: Any, spec: dict[str, Any], label: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise EndpointConfigError(f"{label} must be a whole number") from None
    if not (spec["min"] <= n <= spec["max"]):
        raise EndpointConfigError(
            f"{label} must be between {spec['min']} and {spec['max']}. "
            + spec.get("help", ""))
    return n


def validate_port(port: int | None) -> int | None:
    """A port, or None to follow the protocol default."""
    if port is None:
        return None
    if not (1 <= port <= 65535):
        raise EndpointConfigError("port must be between 1 and 65535")
    return port


def validate_addressing(protocol: str, addressing: dict[str, Any]
                        ) -> dict[str, Any]:
    """Keep the fields this protocol defines, reject values it cannot use."""
    allowed = ADDRESSING_FIELDS.get(protocol, {})
    out: dict[str, Any] = {}
    for key, raw in addressing.items():
        spec = allowed.get(key)
        if spec is None:
            # Silently dropping it would look like a save that worked.
            raise EndpointConfigError(
                f"{protocol} endpoints have no '{key}' setting")
        # `False` is a VALUE here - "verify_tls off" is a decision, not an
        # empty field - so only None and the empty string clear a setting.
        if raw is None or raw == "":
            continue
        kind = spec.get("kind")
        if kind == "text":
            out[key] = str(raw)
        elif kind == "bool":
            if isinstance(raw, bool):
                out[key] = raw
            elif str(raw).lower() in ("true", "false"):
                out[key] = str(raw).lower() == "true"
            else:
                raise EndpointConfigError(
                    f"{spec['label']} is on or off")
        elif kind == "choice":
            if str(raw) not in spec["choices"]:
                raise EndpointConfigError(
                    f"{spec['label']} must be one of "
                    f"{', '.join(spec['choices'])}")
            out[key] = str(raw)
        else:
            out[key] = _int_field(raw, spec, spec["label"])
    return out


def check_credential(protocol: str, cred: dict[str, Any] | None) -> None:
    """A credential belongs to one protocol and cannot be lent to another.

    An SNMP community offered to a Redfish endpoint is not a weaker login, it
    is a guaranteed 401 on every poll for as long as nobody looks.
    """
    if cred is None:
        return
    if cred["protocol"] != protocol:
        raise EndpointConfigError(
            f"that credential is for {cred['protocol']}, "
            f"and this endpoint speaks {protocol}")


def check_addressable(endpoint: dict[str, Any],
                      changing: set[str]) -> None:
    """Where the endpoint's address is not its own to change.

    A field device behind a Modbus gateway or a BACnet router has no IP of its
    own - the gateway's address is the one on the wire, and the unit ID or
    device instance is what selects the device behind it. Letting an operator
    type an address here produces a row that looks configured and is polled at
    an address nothing answers on.
    """
    if endpoint.get("via_endpoint_id") and ({"address", "port"} & changing):
        via = endpoint.get("via_name") or "a gateway"
        raise EndpointConfigError(
            f"this endpoint is reached through {via}, so its address and port "
            f"belong to the gateway. Change the identifier that selects it "
            f"behind the gateway instead, or edit the gateway itself.")


def check_trap_endpoint(endpoint: dict[str, Any], changing: set[str]) -> None:
    """Trap endpoints receive; they are not polled.

    The row records where traps are EXPECTED from. The port that matters for
    receiving them is the collector's listener, which no device row can move -
    editing this one would give an operator every impression of having changed
    where traps arrive, and change nothing at all.
    """
    if endpoint["protocol"] == "snmp_trap" and "port" in changing:
        raise EndpointConfigError(
            "a trap endpoint's port is the collector's listener, not this "
            "device's. Change it in the collector's configuration.")
