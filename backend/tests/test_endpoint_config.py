"""Rules about how a device is reached, and why each one exists.

An endpoint is the one place in this system where a wrong value is invisible.
Nothing fails at save time: the row looks configured, the collector polls it,
and three minutes later an unreachable alarm appears on a device that is
perfectly healthy. Every rule below turns one of those silent failures into a
message at the moment somebody types it.
"""

from __future__ import annotations

import pytest

from app.services import endpoint_config as cfg
from app.services.endpoint_config import EndpointConfigError


# ------------------------------------------------------------------- ports


def test_a_null_port_follows_the_protocol_default():
    """Storing 161 on 1,400 rows makes a fleet-wide correction a migration.

    Left null, the default is one table this code owns.
    """
    assert cfg.validate_port(None) is None
    assert cfg.DEFAULT_PORT["snmp"] == 161
    assert cfg.DEFAULT_PORT["modbus"] == 502
    assert cfg.DEFAULT_PORT["bacnet"] == 47808     # 0xBAC0
    assert cfg.DEFAULT_PORT["redfish"] == 443


@pytest.mark.parametrize("port", [0, -1, 65536, 99999])
def test_impossible_ports_are_refused(port):
    with pytest.raises(EndpointConfigError):
        cfg.validate_port(port)


# -------------------------------------------------------------- addressing


def test_a_modbus_unit_id_stays_inside_the_addressable_range():
    """248-255 are reserved by the specification.

    0 is accepted but means "unset": it is the broadcast address, and the
    collector deliberately does not trust one, because gear that ignores the
    unit id answers a broadcast anyway and would look correctly addressed.
    """
    assert cfg.validate_addressing("modbus", {"unit_id": 12}) == {"unit_id": 12}
    assert cfg.validate_addressing("modbus", {"unit_id": 0}) == {"unit_id": 0}
    for bad in (248, 255, 300, -1):
        with pytest.raises(EndpointConfigError):
            cfg.validate_addressing("modbus", {"unit_id": bad})


def test_a_bacnet_instance_stops_below_the_wildcard():
    """4194303 is the 'unconfigured' value Who-Is uses, so it is never a real
    device's instance number."""
    assert cfg.validate_addressing("bacnet", {"device_instance": 4194302})
    with pytest.raises(EndpointConfigError):
        cfg.validate_addressing("bacnet", {"device_instance": 4194303})


def test_an_mstp_mac_stops_below_the_broadcast_address():
    """255 is the broadcast MAC on an MS/TP trunk and identifies no station."""
    assert cfg.validate_addressing("bacnet", {"mac": 254}) == {"mac": 254}
    with pytest.raises(EndpointConfigError):
        cfg.validate_addressing("bacnet", {"mac": 255})


def test_every_field_offered_is_one_the_collector_reads():
    """The selection rule for this catalogue, kept honest by a test.

    A field the collector ignores is a setting an operator can change with no
    effect, which is worse than one that is missing - the change appears to
    have been made. These key names are the ones the adapters look up in
    Addressing, checked against adapters/{bacnet,modbus,gnmi,redfish}.
    """
    assert set(cfg.ADDRESSING_FIELDS["modbus"]) == {"unit_id", "probe_role"}
    assert set(cfg.ADDRESSING_FIELDS["bacnet"]) == {
        "network", "mac", "device_instance"}
    assert set(cfg.ADDRESSING_FIELDS["gnmi"]) == {"target", "tls", "insecure"}
    assert set(cfg.ADDRESSING_FIELDS["redfish"]) == {
        "scheme", "base", "verify_tls"}


def test_a_switch_is_off_or_on_and_off_is_a_value():
    """"Verify certificate: off" is a decision an operator made.

    Treating it as an empty field would drop it from the payload and hand the
    endpoint back whatever the default is - here, certificate checking on a BMC
    with a self-signed certificate, so every poll fails.
    """
    assert cfg.validate_addressing(
        "redfish", {"verify_tls": False}) == {"verify_tls": False}
    assert cfg.validate_addressing(
        "redfish", {"verify_tls": "true"}) == {"verify_tls": True}
    with pytest.raises(EndpointConfigError):
        cfg.validate_addressing("redfish", {"verify_tls": "maybe"})


def test_a_scheme_outside_the_two_that_exist_is_refused():
    assert cfg.validate_addressing("redfish", {"scheme": "http"})
    with pytest.raises(EndpointConfigError):
        cfg.validate_addressing("redfish", {"scheme": "ftp"})


def test_a_field_the_protocol_does_not_have_is_refused_not_dropped():
    """Silently discarding it would look exactly like a save that worked."""
    with pytest.raises(EndpointConfigError):
        cfg.validate_addressing("redfish", {"unit_id": 3})


def test_blank_values_clear_rather_than_store_an_empty_string():
    assert cfg.validate_addressing("snmp", {"context": ""}) == {}


# ------------------------------------------------------------- credentials


def test_a_credential_cannot_be_lent_to_another_protocol():
    """An SNMP community offered to a Redfish endpoint is not a weaker login.

    It is a guaranteed 401 on every poll, for as long as nobody looks at that
    device's endpoint table.
    """
    with pytest.raises(EndpointConfigError):
        cfg.check_credential("redfish", {"protocol": "snmp", "name": "ro"})
    cfg.check_credential("redfish", {"protocol": "redfish", "name": "bmc"})
    cfg.check_credential("snmp", None)          # needing none is legitimate


# ----------------------------------------------------- who owns the address


def test_a_device_behind_a_gateway_does_not_own_its_address():
    """A Modbus slave on an RS-485 trunk has no IP.

    The gateway's address is what is on the wire and the unit ID selects the
    device behind it. Typing an address here produces a row that looks
    configured and is polled at an address nothing answers on.
    """
    behind = {"protocol": "modbus", "via_endpoint_id": "gw-1",
              "via_name": "MOXA-DC2-EL"}
    with pytest.raises(EndpointConfigError) as exc:
        cfg.check_addressable(behind, {"address"})
    assert "MOXA-DC2-EL" in str(exc.value)      # name the gateway, not just the rule

    # The identifier that DOES select it stays editable.
    cfg.check_addressable(behind, {"addressing"})


def test_a_directly_addressed_endpoint_is_unaffected():
    cfg.check_addressable({"protocol": "snmp", "via_endpoint_id": None},
                          {"address", "port"})


def test_a_trap_endpoints_port_belongs_to_the_collector():
    """The row records where traps are expected FROM; the port that decides
    where they arrive is the collector's listener.

    Editing this one would give every impression of moving the trap port and
    move nothing - the failure mode being silence, which is what a trap plane
    looks like when it is broken anyway.
    """
    trap = {"protocol": "snmp_trap", "via_endpoint_id": None}
    with pytest.raises(EndpointConfigError) as exc:
        cfg.check_trap_endpoint(trap, {"port"})
    assert "collector" in str(exc.value)

    # Everything else about a trap endpoint stays editable.
    cfg.check_trap_endpoint(trap, {"enabled", "credential_id"})
    cfg.check_trap_endpoint({"protocol": "snmp"}, {"port"})
