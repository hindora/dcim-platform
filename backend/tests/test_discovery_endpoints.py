"""Promotion wires up monitoring: which agent, which profile, which credential.

The rules are the importer's, reached through the same functions - the two roads
into inventory must not disagree about how a Raritan strip or a BMC is polled -
and the credential rules are the security half: the collector names what worked
by reference, a secret typed into the dialog is never echoed or overwritten, and
only the machine being promoted can be wired.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import audit
from app.services import discovery_endpoints as de

APP = Path(__file__).resolve().parents[1] / "app"
SRC = (APP / "services" / "discovery_endpoints.py").read_text(encoding="utf-8")
SVC = (APP / "services" / "discovery.py").read_text(encoding="utf-8")


def _probe(protocol: str, access: dict | None = None, address: str = "10.51.11.40"):
    return {"id": "c1", "protocol": protocol, "address": address,
            "identity": {"access": access} if access is not None else {}}


# --------------------------------------------------------------------- roles

def test_a_server_snmp_agent_beside_redfish_is_its_bmc():
    """The controller serves both. Called an OS agent, it was polled with the
    host-resources profile and asked an iDRAC for hrStorage it does not have."""
    assert de.role_for("snmp", "server", "10.51.11.40", {"10.51.11.40"}) == "bmc"
    assert de.role_for("snmp", "server", "10.50.11.40", {"10.51.11.40"}) == "os_agent"
    assert de.role_for("redfish", "server", "10.51.11.40", set()) == "bmc"
    assert de.role_for("snmp", "pdu", "10.52.11.9", set()) == "native_card"


@pytest.mark.parametrize(("protocol", "role", "dtype", "vendor", "want"), [
    ("redfish", "bmc", "server", "Dell Inc.", "redfish-60s"),
    ("snmp", "bmc", "server", "Dell Inc.", "snmp-bmc-120s"),
    ("snmp", "os_agent", "server", "Dell Inc.", "snmp-server-120s"),
    # Vendor-split families: the MIBs share no OIDs.
    ("snmp", "native_card", "pdu", "Raritan", "snmp-pdu-raritan-120s"),
    ("snmp", "native_card", "pdu", "APC", "snmp-pdu-apc-120s"),
    ("snmp", "native_card", "switch", "Cisco", "snmp-network-cisco-600s"),
    ("snmp", "native_card", "firewall", "Palo Alto Networks", "snmp-network-host-600s"),
    ("snmp", "native_card", "sensor", None, "snmp-sensor-10s"),
    ("snmp", "native_card", "ups", "Eaton", "snmp-power-120s"),
])
def test_the_profile_is_the_importers(protocol, role, dtype, vendor, want):
    assert de.default_profile(protocol, role, dtype, vendor) == want


# --------------------------------------------------------------- credentials

def test_the_address_convention_is_proposed_only_when_the_sweep_proved_it():
    cred, _ = de.suggested_credential(_probe("snmp", {"community": "address"}))
    assert cred == {"mode": "address"}
    # A configured community is named by index - the value never left the
    # collector - so a person has to choose the matching credential.
    cred, why = de.suggested_credential(
        _probe("snmp", {"community": "configured", "community_index": "2"}))
    assert cred is None and "#2" in why


def test_a_redfish_login_is_never_guessed():
    """admin/password is the SIMULATOR's login. Proposing it to a real BMC is a
    failed-login alarm, and on some a lockout."""
    for access in ({"credential": "configured", "credential_index": "0"},
                   {"credential": "none"}, None):
        cred, why = de.suggested_credential(_probe("redfish", access))
        assert cred is None, access
        assert why


def test_an_old_collector_that_recorded_nothing_proposes_nothing():
    cred, why = de.suggested_credential(_probe("snmp", None))
    assert cred is None and "did not record" in why


def test_a_new_credential_never_replaces_one_in_use():
    body = SRC[SRC.index("async def _credential("):SRC.index("async def _credential_by_name")]
    assert "already exists; choose it under" in body
    assert "ON CONFLICT" not in body, "an upsert would rotate a live secret"


def test_the_address_credential_is_the_importers_row():
    """Same name as the importer's, reused as it stands - somebody may have
    rotated it since, and a promotion is not the place to undo that."""
    assert 'name = f"snmp-v2c-{address}"' in SRC


def test_a_promote_body_is_scrubbed_before_it_is_audited():
    body = {"name": "srv-1", "endpoints": [
        {"candidate_id": "c1",
         "credential": {"mode": "new", "username": "root", "password": "calvin"}}]}
    out = audit.scrub(body)
    assert "calvin" not in repr(out)


# ---------------------------------------------------------------- the machine

def test_only_the_promoted_machine_can_be_wired():
    assert "that probe is not part of the machine being" in SRC
    probes = SRC[SRC.index("async def machine_probes"):SRC.index("async def plan")]
    # Dismissed probes stay dismissed.
    assert "status IN ('new', 'promoted')" in probes


def test_promotion_settles_every_probe_of_the_machine():
    """Promoting a server's Redfish half left its SNMP half `new` and unmatched,
    and the page went on calling the machine "not in inventory"."""
    monitor = SVC[SVC.index("async def _monitor"):SVC.index("async def monitoring_plan")]
    assert "settle_probes" in monitor
    settle = SRC[SRC.index("async def settle_probes"):]
    assert "matched_device_id = CAST(:dev AS uuid)" in settle


def test_bulk_creates_only_what_the_evidence_proves():
    monitor = SVC[SVC.index("async def _monitor"):SVC.index("async def monitoring_plan")]
    assert 'if item["suggested_credential"]:' in monitor
    assert "skipped.append" in monitor


def test_the_sweep_keeps_how_it_got_in():
    rec = SVC[SVC.index("async def record_results"):]
    assert 'identity = {**identity, "access": dict(r["access"])}' in rec
    col = (APP / "api" / "v1" / "collector.py").read_text(encoding="utf-8")
    assert "access: dict[str, str]" in col
