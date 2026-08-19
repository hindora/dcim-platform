"""Endpoint derivation from simulator device records.

These assert the addressing rules read out of the simulator source. Getting any
of them wrong produces devices that look permanently dead, which is exactly the
failure this test suite exists to prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
    assert eps[0].via_address == "10.52.11.70"
    # The unit id says WHICH device on the trunk; the probe role says what it
    # measures. Both are required: a transmitter publishes one nameless
    # process value, and device_type "sensor" covers an RTD and a flow meter
    # alike, so without the role the adapter cannot even pick a register map.
    assert eps[0].addressing == {"unit_id": 7, "probe_role": "chw_supply"}


def test_native_modbus_gear_gets_an_endpoint_without_a_role():
    """The export sets modbus_role only for the serial trunk.

    Keying on the role alone created endpoints for the twelve transmitters and
    none of the thirty meters, switchgear, gensets, transfer switches and UPS
    that carry the site's electrical telemetry - and nothing failed, because a
    device with no endpoint is simply never polled.
    """
    meter = {
        "id": "cc33", "device_type": "utility_feed", "name": "UTIL1-DC1-UR",
        "mgmt_ip": "10.52.14.47",
    }
    eps = derive_endpoints(meter, include_protocols=frozenset({"modbus"}))
    assert len(eps) == 1
    assert eps[0].role == "native_card"
    assert eps[0].address == "10.52.14.47"
    assert eps[0].port == 502
    assert eps[0].addressing == {"unit_id": 1}


def test_modbus_gateway_is_recorded_but_not_polled():
    """A serial gateway is not a meter.

    It is recorded so the RTU slaves have something to hang via_endpoint_id
    from, and so an unreachable trunk is attributable to the box in front of
    it - but polling it would read registers it does not have.
    """
    gw = {
        "id": "dd44", "device_type": "modbus_gateway", "name": "MBGW1-DC1-CP",
        "modbus_role": "gateway", "mgmt_ip": "10.52.14.19",
    }
    eps = derive_endpoints(gw, include_protocols=frozenset({"modbus"}))
    assert len(eps) == 1
    assert eps[0].role == "gateway"
    assert eps[0].enabled is False


def test_gnmi_is_dialled_at_the_device_itself(sim_switch_device):
    """Each device serves gNMI on its own address.

    That is how real gear works and what this plane does - 46 listeners on 46
    device IPs. An earlier version dialled one shared server and selected the
    device with prefix.target; that shape exists (a collector-facing gNMI
    gateway) but is not what is in front of us, and every endpoint would have
    pointed at a port nothing was listening on.

    The target is still sent, because gear that fronts several devices needs
    it and gear that serves only itself ignores it.
    """
    eps = derive_endpoints(sim_switch_device,
                           include_protocols=frozenset({"gnmi"}),
                           )
    assert len(eps) == 1
    assert eps[0].address == "10.51.11.2"
    assert eps[0].addressing["target"] == "10.51.11.2"
    # NOT the device's own gnmi_port field, which says 57400 on every device
    # while the listeners are on 50051. The port the server actually binds is
    # the authoritative one.
    assert sim_switch_device["gnmi_port"] == 57400
    assert eps[0].port == 50051
    assert derive_endpoints(sim_switch_device,
                            include_protocols=frozenset({"gnmi"}),
                            gnmi_port=9339)[0].port == 9339


def test_redfish_tls_verification_is_per_endpoint(sim_server_device):
    eps = derive_endpoints(sim_server_device, include_protocols=frozenset({"redfish"}))
    assert len(eps) == 1
    assert eps[0].port == 8443
    assert eps[0].addressing["verify_tls"] is False
    assert eps[0].credential_payload["username"] == "admin"


def test_phase_1_default_creates_snmp_only(sim_server_device):
    eps = derive_endpoints(sim_server_device)
    assert {e.protocol for e in eps} == {"snmp"}


def test_gnmi_is_only_for_fabric_devices():
    """gNMI is a fabric feature, not something every network box speaks.

    Switches and routers ship it; firewalls and load balancers expose vendor
    APIs instead, and console or OOB switches usually speak SNMP and nothing
    else. The plane agrees - 46 targets, every one a switch or a router - and
    creating endpoints for the rest produced permanently reconnecting sessions
    whose only message was that nothing was listening.
    """
    for dtype in ("firewall", "load_balancer", "oob_switch"):
        dev = {"id": "x", "device_type": dtype, "name": f"{dtype}-1",
               "mgmt_ip": "10.51.13.12"}
        eps = derive_endpoints(dev, include_protocols=frozenset({"gnmi"}))
        assert eps == [], f"{dtype} should not get a gNMI endpoint"

    for dtype in ("switch", "router"):
        dev = {"id": "x", "device_type": dtype, "name": f"{dtype}-1",
               "mgmt_ip": "10.51.13.10"}
        eps = derive_endpoints(dev, include_protocols=frozenset({"gnmi"}))
        assert len(eps) == 1, f"{dtype} should get a gNMI endpoint"


# --- assignment ETag ---------------------------------------------------------

def _assignment(interval_s: int = 30, timeout_ms: int = 3000,
                metric_groups: list[str] | None = None):
    from app.schemas import (Assignment, AssignmentEndpoint, AssignmentPoll)
    return Assignment(
        version=1, generated_at=datetime(2026, 8, 19, tzinfo=UTC),
        collector_id="c1",
        endpoints=[AssignmentEndpoint(
            id="e1", device_id="d1", device_name="sw1", device_type="switch",
            protocol="snmp", role="os_agent", address="10.50.1.1", port=161,
            addressing={},
            poll=AssignmentPoll(
                interval_s=interval_s, timeout_ms=timeout_ms, retries=2,
                metric_groups=metric_groups or ["system", "interfaces"],
                push_enabled=False),
        )])


def test_etag_changes_when_a_poll_profile_changes():
    """A profile edit must invalidate the ETag.

    interval_s is served from poll_profile, which the endpoint rows only
    reference, so the assignment version (built from device_endpoint.updated_at)
    does not move when a profile is edited. If the ETag ignored the poll fields
    too, every collector would answer its own If-None-Match with a 304 and keep
    polling at the old interval until an unrelated endpoint edit or a restart -
    a config change that silently does nothing.
    """
    from app.services.collector import etag_for

    base = etag_for(_assignment(interval_s=30))
    assert etag_for(_assignment(interval_s=120)) != base, \
        "raising the interval left the ETag unchanged; collectors would 304"
    assert etag_for(_assignment(timeout_ms=9000)) != base
    assert etag_for(_assignment(metric_groups=["system"])) != base
    # Same content, same ETag - otherwise every refresh re-downloads.
    assert etag_for(_assignment(interval_s=30)) == base
