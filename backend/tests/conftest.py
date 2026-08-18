"""Test configuration.

Settings are populated before any app module imports, because ``Settings``
refuses to construct without its secrets - which is the behaviour we want in
production and merely needs arranging here.
"""

from __future__ import annotations

import base64
import os

os.environ.setdefault("DCIM_JWT_SECRET", "test-secret-not-used-anywhere-real")
os.environ.setdefault("DCIM_COLLECTOR_TOKEN", "test-collector-token")
os.environ.setdefault("DCIM_CREDENTIAL_KEY",
                      base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("DCIM_DATABASE_URL",
                      "postgresql+asyncpg://dcim:dcim@localhost:5432/dcim_test")
os.environ.setdefault("DCIM_ADMIN_PASSWORD", "test-admin-password")

import pytest


@pytest.fixture
def sim_server_device() -> dict:
    """A server record shaped exactly like the simulator's topology export.

    Note the two addresses: the OS SNMP agent answers on the production NIC and
    the BMC is a separate controller on the management network.
    """
    return {
        "id": "fa03fbfd",
        "name": "SRV01-DC1-HA-R2-01",
        "device_type": "server",
        "vendor": "Supermicro",
        "model_name": "Supermicro SYS-121H-TNR LCC",
        "ip_address": "10.50.11.19",
        "mgmt_ip": "10.51.11.25",
        "snmp_port": 161,
        "gnmi_port": 57400,
        "snmp_community": "10.51.11.25",
        "datacenter": "DC1",
        "datacenter_city": "Chicago",
        "country": "USA",
        "room": "Server Hall A",
        "floor": "1",
        "rack_row": 2,
        "rack_num": 1,
        "rack_unit": 1,
        "rack_facing": "N",
        "power_draw_w": 800,
        "interfaces": [],
        "outlets": [],
        "psus": [],
    }


@pytest.fixture
def sim_chiller_device() -> dict:
    """A chiller: BACnet-only, no SNMP card, floor-standing."""
    return {
        "id": "8f7731aa",
        "name": "CH01-DC1-PLANT",
        "device_type": "chiller",
        "vendor": "Carrier",
        "model_name": "Carrier 19XR",
        "ip_address": "",
        "mgmt_ip": "10.52.11.40",
        "datacenter": "DC1",
        "room": "Plant Room 1",
    }


@pytest.fixture
def sim_switch_device() -> dict:
    return {
        "id": "ec012c83",
        "name": "LF01-DC1-HA-R2-01",
        "device_type": "switch",
        "vendor": "Arista Networks",
        "model_name": "Arista DCS-7050SX3",
        "ip_address": "10.50.11.2",
        "mgmt_ip": "10.51.11.2",
        "snmp_port": 161,
        "gnmi_port": 57400,
        "datacenter": "DC1",
        "room": "Server Hall A",
        "rack_row": 2,
        "rack_num": 1,
        "rack_unit": 12,
    }
