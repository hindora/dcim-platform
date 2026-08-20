"""Discovery classification and validation.

The sweep itself is Go and lives in the collector; this is the half that
decides what an answer means.
"""

from __future__ import annotations

import pytest

from app.services import discovery as d


def ident(descr: str) -> dict[str, str]:
    return {"sysDescr": descr, "sysName": "x"}


# --- classification ----------------------------------------------------------

@pytest.mark.parametrize("descr,expected", [
    ("iDRAC9 6.10.30.00 — Dell Technologies Dell PowerEdge", "server"),
    ("iLO 6 1.55 — Hewlett Packard Enterprise HPE ProLiant", "server"),
    ("XClarity Controller 22A — Lenovo ThinkSystem", "server"),
    ("Supermicro BMC 01.04.16 — Supermicro SYS-220U-TNR BMC", "server"),
    ("Cisco NX-OS Software", "switch"),
    ("APC Rack PDU 2G", "pdu"),
    ("Eaton UPS 93PM", "ups"),
])
def test_device_type_is_guessed_from_sysdescr(descr, expected):
    assert d.classify(ident(descr))[0] == expected


@pytest.mark.parametrize("descr,vendor", [
    ("iDRAC9 — Dell PowerEdge", "Dell"),
    ("Supermicro BMC 01.04.16", "Supermicro"),
    ("Cisco IOS", "Cisco"),
    ("APC Rack PDU", "Schneider Electric"),
])
def test_vendor_is_guessed_too(descr, vendor):
    assert d.classify(ident(descr))[1] == vendor


def test_an_unrecognised_device_gets_no_guess_rather_than_a_wrong_one():
    """A candidate with no suggestion is honest; a wrong one costs an operator
    the time it takes to notice."""
    assert d.classify(ident("Frobnicator 9000")) == (None, None)


def test_an_empty_identity_classifies_to_nothing():
    assert d.classify({}) == (None, None)
    assert d.classify({"sysDescr": ""}) == (None, None)


# --- run validation ----------------------------------------------------------

@pytest.mark.asyncio
async def test_a_run_needs_a_subnet():
    with pytest.raises(d.DiscoveryError):
        await d.create_run(None, method="snmp_sweep", subnets=[])


@pytest.mark.asyncio
async def test_a_malformed_cidr_is_refused_at_request_time():
    """Not an hour later on a collector, where nobody is watching."""
    for bad in ("10.51.11.0", "not-a-network", "10.51.11.0/", "10.51.11.0/24/8"):
        with pytest.raises(d.DiscoveryError):
            await d.create_run(None, method="snmp_sweep", subnets=[bad])


@pytest.mark.asyncio
async def test_only_implemented_methods_are_accepted():
    """bacnet_whois and redfish_probe are the same shape but do not exist yet,
    and accepting them would queue a run nothing will ever claim."""
    with pytest.raises(d.DiscoveryError):
        await d.create_run(None, method="bacnet_whois", subnets=["10.51.0.0/24"])
