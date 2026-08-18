"""Endpoint derivation from simulator device records.

These assert the addressing rules read out of the simulator source. Getting any
of them wrong produces devices that look permanently dead, which is exactly the
failure this test suite exists to prevent.
"""

from __future__ import annotations

from app.importer.endpoints import (
    NO_SNMP_TYPES,
    bmc_address,
    derive_endpoints,
    snmp_address,
)


def test_server_has_two_separate_snmp_agents(sim_server_device):
    """OS agent on the production NIC, BMC agent on the management IP."""
    eps = derive_endpoints(sim_server_device)
    snmp = [e for e in eps if e.protocol == "snmp"]
    assert len(snmp) == 2

    os_agent = next(e for e in snmp if e.role == "os_agent")
    bmc = next(e for e in snmp if e.role == "bmc")
    assert os_agent.address == "10.50.11.19"
    assert bmc.address == "10.51.11.25"
    assert os_agent.address != bmc.address


def test_community_is_the_snmp_address_never_public(sim_server_device):
    """A wrong community is a SILENT DROP that looks like a dead device."""
    eps = derive_endpoints(sim_server_device)
    for e in (e for e in eps if e.protocol == "snmp"):
        assert e.credential_payload == {"community": e.address}
        assert e.credential_payload["community"] != "public"


def test_network_gear_answers_snmp_on_the_management_ip(sim_switch_device):
    eps = derive_endpoints(sim_switch_device)
    snmp = [e for e in eps if e.protocol == "snmp"]
    assert len(snmp) == 1
    assert snmp[0].role == "native_card"
    assert snmp[0].address == "10.51.11.2"        # mgmt, not 10.50.11.2
    assert snmp[0].credential_payload == {"community": "10.51.11.2"}


def test_snmp_address_rules():
    server = {"device_type": "server", "ip_address": "10.50.0.5", "mgmt_ip": "10.51.0.5"}
    switch = {"device_type": "switch", "ip_address": "10.50.0.2", "mgmt_ip": "10.51.0.2"}
    assert snmp_address(server) == "10.50.0.5"
    assert snmp_address(switch) == "10.51.0.2"
    assert bmc_address(server) == "10.51.0.5"
    assert bmc_address(switch) is None


def test_bacnet_only_plant_gets_no_snmp_endpoint(sim_chiller_device):
    """Real chillers carry no SNMP card; the BMS gateways their points.

    Creating an SNMP endpoint would produce a permanent false unreachable alarm.
    """
    assert sim_chiller_device["device_type"] in NO_SNMP_TYPES
    eps = derive_endpoints(sim_chiller_device, include_protocols=frozenset({"snmp"}))
    assert eps == []


def test_mstp_field_device_is_reached_through_its_router():
    valve = {
        "id": "aa11", "device_type": "valve", "name": "CV01",
        "mstp_net": 2001, "mstp_mac": 12, "mstp_router_ip": "10.52.11.60",
    }
    eps = derive_endpoints(valve, include_protocols=frozenset({"bacnet"}))
    assert len(eps) == 1
    ep = eps[0]
    assert ep.role == "field_device"
    # No IP of its own: the router's address plus (network, MAC).
    assert ep.address == "10.52.11.60"
    assert ep.addressing == {"network": 2001, "mac": 12}
    assert ep.via_address == "10.52.11.60"


def test_modbus_rtu_slave_is_reached_through_its_gateway():
    transmitter = {
        "id": "bb22", "device_type": "sensor", "name": "CHWS-TT-01",
        "modbus_role": "rtu_slave", "modbus_unit_id": 7,
        "modbus_gateway_ip": "10.52.11.70",
    }
    eps = derive_endpoints(transmitter, include_protocols=frozenset({"modbus"}))
    assert len(eps) == 1
    assert eps[0].address == "10.52.11.70"
    assert eps[0].addressing == {"unit_id": 7}
    assert eps[0].via_address == "10.52.11.70"


def test_gnmi_target_is_the_device_but_address_is_the_server(sim_switch_device):
    """One gRPC server serves every target; selection is prefix.target."""
    eps = derive_endpoints(sim_switch_device,
                           include_protocols=frozenset({"gnmi"}),
                           gnmi_server_host="192.0.2.10")
    assert len(eps) == 1
    assert eps[0].address == "192.0.2.10"
    assert eps[0].addressing["target"] == "10.51.11.2"
    assert eps[0].port == 57400


def test_redfish_tls_verification_is_per_endpoint(sim_server_device):
    eps = derive_endpoints(sim_server_device, include_protocols=frozenset({"redfish"}))
    assert len(eps) == 1
    assert eps[0].port == 8443
    assert eps[0].addressing["verify_tls"] is False
    assert eps[0].credential_payload["username"] == "admin"


def test_phase_1_default_creates_snmp_only(sim_server_device):
    eps = derive_endpoints(sim_server_device)
    assert {e.protocol for e in eps} == {"snmp"}
