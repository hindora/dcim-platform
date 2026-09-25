"""Discovery classification and validation.

The sweep itself is Go and lives in the collector; this is the half that
decides what an answer means.
"""

from __future__ import annotations

from pathlib import Path

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


@pytest.mark.parametrize("descr,expected", [
    # Every string here is what the gear itself says, in the words the vendor uses.
    ("ASCO 7000 Series automatic transfer switch, 4000A, 5350 controller, "
     "ASCO Connectivity Module, Modbus TCP", "ats"),
    ("ASCO 7000 Series paralleling switchgear, generator paralleling controls, "
     "ASCO Connectivity Module, Modbus TCP", "switchgear"),
    ("Eaton Power Xpert Gateway PXG 900, fw 2.1.5, INCOM-to-Ethernet, "
     "Magnum DS low-voltage switchgear, 4000A, Digitrip 1150 trip units",
     "switchgear"),
    ("Eaton Power Xpert Gateway PXG 900, fw 2.1.5, Modbus-to-Ethernet, "
     "Freedom 2100 motor control center, 1600A, C441 motor protection relays",
     "mcc"),
    ("Eaton Power Xpert Gateway PXG 900, fw 2.1.5, Modbus-to-Ethernet, "
     "Pow-R-Line 3a panelboard, 150A, Power Xpert Meter 2000 branch metering",
     "mpp"),
    ("CoolIT CHx80 coolant distribution unit, 80 kW liquid-to-liquid, "
     "onboard controller fw 2.4.1, Modbus TCP to the BMS", "cdu"),
    ("LOYTEC LINX-151 L-INX automation server, BACnet/IP to BACnet MS/TP router, "
     "fw 7.4.2", "bacnet_router"),
    ("Moxa MGate MB3480, 4-port Modbus RTU/ASCII to Modbus TCP gateway, fw 4.2",
     "modbus_gateway"),
])
def test_facility_gear_is_classified_from_what_it_calls_itself(descr, expected):
    """22 devices used to answer with sysDescr "Generic Device" and no guess at all."""
    assert d.classify(ident(descr))[0] == expected


def test_the_facility_patterns_come_before_the_network_ones():
    """The whole reason this ordering is load-bearing, stated as a test.

    An ASCO 7000 calls itself a "transfer SWITCH" and a LOYTEC L-INX calls itself a
    BACnet ROUTER. Matched against the network patterns first, a 4000 A transfer
    switch is filed as an access switch and a BACnet router as a network router -
    which is worse than the "Generic Device" both used to send, because a wrong
    suggestion gets accepted and an absent one gets looked at.
    """
    types = [t for _pattern, t in d._TYPE_HINTS]
    for facility, network in [("ats", "switch"), ("switchgear", "switch"),
                              ("bacnet_router", "router"),
                              ("modbus_gateway", "router")]:
        assert types.index(facility) < types.index(network), (
            f"{facility} must be tried before {network}")


@pytest.mark.parametrize("descr,vendor", [
    ("ASCO 7000 Series automatic transfer switch", "ASCO Power Technologies"),
    ("Moxa MGate MB3480, Modbus TCP gateway", "Moxa"),
    ("LOYTEC LINX-151 L-INX automation server", "Loytec"),
    ("CoolIT CHx80 coolant distribution unit", "CoolIT Systems"),
])
def test_facility_vendors_are_recognised(descr, vendor):
    assert d.classify(ident(descr))[1] == vendor


def test_a_gateway_is_classified_as_the_gateway_not_as_what_is_behind_it():
    """A sweep that finds an MGate has found an MGate.

    The 12 plant instruments behind these gateways have no addresses of their own,
    so classifying the responder as a sensor - or letting an operator read it that
    way - would turn one reachable gateway into twelve healthy instruments that
    nobody has actually heard from.
    """
    mgate = ("Moxa MGate MB3480, 4-port Modbus RTU/ASCII to Modbus TCP gateway, "
             "fw 4.2")
    assert d.classify(ident(mgate))[0] == "modbus_gateway"
    loytec = ("LOYTEC LINX-151 L-INX automation server, BACnet/IP to BACnet MS/TP "
              "router, fw 7.4.2")
    assert d.classify(ident(loytec))[0] == "bacnet_router"


@pytest.mark.parametrize("descr,expected", [
    # Real IOS/IOS-XE/IOS-XR strings, where the image names the platform.
    ("Cisco IOS Software [Amsterdam], ISR Software "
     "(X86_64_LINUX_IOSD-UNIVERSALK9-M), Version 17.9.4a, RELEASE SOFTWARE (fc3)",
     "router"),
    ("Cisco IOS Software [Amsterdam], ASR1000 Software "
     "(X86_64_LINUX_IOSD-UNIVERSALK9-M), Version 17.9.4a, RELEASE SOFTWARE (fc3)",
     "router"),
    ("Cisco IOS XR Software (Cisco ASR9K Series), Version 7.9.1[Default]", "router"),
    ("Cisco IOS XE Software, Catalyst L3 Switch Software (CAT9K_IOSXE), "
     "Version 17.12.1, RELEASE SOFTWARE (fc3)", "switch"),
    ("Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), "
     "Version 15.2(7)E6, RELEASE SOFTWARE (fc2)", "switch"),
    ("Cisco IOS Software, C1000 Software (C1000-UNIVERSALK9-M), "
     "Version 15.2(7)E6, RELEASE SOFTWARE (fc2)", "switch"),
    ("Cisco NX-OS(tm) n9000, Software (n9000-dk9), Version 10.4(1)", "switch"),
])
def test_a_cisco_router_is_not_a_switch(descr, expected):
    """The switch pattern used to contain a bare "ios".

    That said "anything running IOS is a switch", which filed every Cisco IOS router
    in the estate as one. IOS is a VENDOR signal - the vendor list has it - and the
    type signal is the image name beside it.
    """
    assert d.classify(ident(descr))[0] == expected


def test_running_ios_is_not_by_itself_evidence_of_anything(descr="Cisco IOS Software"):
    """A bare OS name identifies the vendor and no more.

    Kept as its own test because it is the assumption that caused the bug, and
    because re-adding "ios" to the type hints would make every router a switch again
    while leaving all the parametrised cases above passing.
    """
    dtype, vendor = d.classify(ident(descr))
    assert vendor == "Cisco"
    assert dtype is None, f"a bare OS string suggested {dtype}"


PAN_DESCR = "Palo Alto Networks PA-5220 series firewall, PAN-OS 11.0.2"
F5_DESCR = ("Linux f5-i5800-dc1.mgmt 3.10.0-1160.45.1.el7.ve.x86_64 #1 SMP "
            "Thu Oct 19 2023 x86_64")


@pytest.mark.parametrize("identity,expected", [
    ({"sysDescr": PAN_DESCR}, "firewall"),
    ({"sysObjectID": "1.3.6.1.4.1.25461"}, "firewall"),
    ({"sysDescr": PAN_DESCR, "sysObjectID": "1.3.6.1.4.1.25461"}, "firewall"),
    ({"sysDescr": F5_DESCR, "sysObjectID": "1.3.6.1.4.1.3375"}, "load_balancer"),
    ({"sysObjectID": ".1.3.6.1.4.1.3375"}, "load_balancer"),
    ({"sysDescr": "F5 Networks BIG-IP, TMOS Version 17.1.0"}, "load_balancer"),
])
def test_a_firewall_and_a_load_balancer_are_not_servers(identity, expected):
    """Both were arriving with no type and no vendor at all."""
    assert d.classify(identity)[0] == expected


SONIC = ("Enterprise SONiC Distribution by Dell Technologies - 4.2.0 - "
         "HwSku: DellEMC-S5248f-P-25G-DPB - Distribution: Debian - "
         "Kernel: 5.10.0-18-2-amd64")
DNOS = "Dell EMC Networking N3248TE-ON, DNOS 6.5.1.9, 48-port GbE + 4-port SFP+"


@pytest.mark.parametrize("descr", [SONIC, DNOS])
def test_a_dell_switch_is_classified_from_its_network_os(descr):
    """Dell names the NOS, not the equipment class.

    Enterprise SONiC reports its HwSku - the platform identifier - and the N-series
    reports "Dell EMC Networking ... DNOS". Neither contains "switch", which is why
    both families arrived with no suggested type at all.
    """
    dtype, vendor = d.classify(ident(descr))
    assert dtype == "switch"
    assert vendor == "Dell"


def test_a_dell_server_is_not_filed_as_a_switch():
    """The regression the Dell fix had to avoid, pinned.

    Dell's networking arc is 674.10895 and 82 PowerEdge SERVERS in the simulated
    estate carry 674.10895.3000, inherited from the vendor fallback. The switch
    patterns are tried BEFORE the server ones, so matching that arc here - the obvious
    way to catch a Dell switch whose text says nothing useful - would have filed every
    Dell server as a switch. The hints match the NOS name instead.
    """
    server = {"sysDescr": "Dell PowerEdge R760 running Red Hat Enterprise Linux 9.2",
              "sysObjectID": "1.3.6.1.4.1.674.10895.3000"}
    assert d.classify(server)[0] == "server"

    # And the BMC in front of it, which is what a sweep usually reaches first.
    idrac = {"sysDescr": "iDRAC9 7.10.30.00 - Dell Technologies Dell PowerEdge R760",
             "sysObjectID": "1.3.6.1.4.1.674.10895.3000"}
    assert d.classify(idrac)[0] == "server"


def test_an_f5_cannot_be_identified_from_sysdescr_alone():
    """The realistic part, and the reason the OID is in the pattern.

    TMOS runs on a Linux host and BIG-IP answers sysDescr with that host's uname - no
    "BIG-IP", no "load balancer", nothing about what the box is for. Read on sysDescr
    alone it is a Linux server, and that is not a bug in the classifier: it is why F5
    monitoring keys on sysObjectID and the F5-BIGIP-SYSTEM-MIB instead.

    Pinned as a POSITIVE assertion rather than left implicit, so that nobody "fixes"
    it by writing a friendlier sysDescr into the simulator and quietly teaching the
    collector that sysDescr is always enough.
    """
    assert d.classify({"sysDescr": F5_DESCR}) == ("server", None)
    # And with the one leaf that carries the answer, it is right.
    assert d.classify({"sysDescr": F5_DESCR,
                       "sysObjectID": "1.3.6.1.4.1.3375"}) == ("load_balancer", "F5")


def test_the_load_balancer_pattern_beats_the_server_one():
    """Order, not cleverness, is what makes the F5 case work.

    The uname matches `linux`, so if the server patterns were tried first the OID
    would never get a look in.
    """
    types = [t for _pattern, t in d._TYPE_HINTS]
    assert types.index("load_balancer") < types.index("server")


@pytest.mark.parametrize("oid", ["1.3.6.1.4.1.33751.1", "1.3.6.1.4.1.254612.1"])
def test_an_enterprise_number_does_not_match_a_longer_one(oid):
    """3375 must not match inside 33751, nor 25461 inside 254612.

    Same hazard as the "n9000" token that matched inside "ION9000" and filed a power
    meter as a Nexus. An OID pattern is a number, so it needs the same guard.
    """
    assert d.classify({"sysObjectID": oid}) == (None, None)


def test_a_platform_number_is_not_matched_unanchored():
    """"n9000" was in the switch pattern and matched inside "ION9000".

    A Schneider PowerLogic ION9000 revenue meter classified as a Cisco Nexus. The
    token was redundant - every Nexus string carries "nx-os" - so the substring
    hazard bought nothing. Any unanchored platform NUMBER added here needs this
    check run against the rest of the estate first.
    """
    meter = ("Schneider Electric PowerLogic ION9000, revenue-grade power quality "
             "meter, fw 4.2.1, utility service-entrance metering, Modbus TCP")
    assert d.classify(ident(meter))[0] != "switch"


def test_an_unrecognised_device_gets_no_guess_rather_than_a_wrong_one():
    """A candidate with no suggestion is honest; a wrong one costs an operator
    the time it takes to notice."""
    assert d.classify(ident("Frobnicator 9000")) == (None, None)


def test_an_empty_identity_classifies_to_nothing():
    assert d.classify({}) == (None, None)
    assert d.classify({"sysDescr": ""}) == (None, None)


# --- the upsert ---------------------------------------------------------------

REPO = (Path(__file__).resolve().parents[1] / "app" / "repositories"
        / "discovery.py").read_text(encoding="utf-8")


def test_a_reswept_responder_does_not_keep_its_first_guess():
    """The suggestion is derived from the identity, so it has to move with it.

    The upsert replaced `identity` and `serial` on conflict and left the suggestions
    alone, so a row ended up carrying a guess its own identity column contradicted. A
    device that is re-imaged, re-badged, re-purposed - or simply identified correctly
    after a fix - kept the wrong suggestion for ever.

    Found live: Liebert CRAHs and Eaton panelboards whose sysDescr had been corrected
    still read "ups" on the page, while every address being swept for the FIRST time
    classified correctly in the same run.
    """
    # The ON CONFLICT clause of the candidate upsert.
    start = REPO.index("ON CONFLICT (address, protocol)")
    clause = REPO[start:REPO.index("RETURNING", start)]

    for column in ("identity", "serial", "matched_device_id",
                   "suggested_device_type", "suggested_vendor", "last_seen"):
        assert column in clause, f"a re-sweep does not refresh {column}"


def test_going_quiet_is_judged_only_where_the_sweep_actually_looked():
    """The difference between "asked and silent" and "never asked".

    A run records the subnets it swept, and mark_gone may only judge addresses inside
    them. Without that guard a sweep of the electrical plane would mark the whole IT
    plane gone - a page that invented 500 dead devices from one /27 would be worse
    than the stale row it was written to remove.
    """
    start = REPO.index("async def mark_gone")
    body = REPO[start:REPO.index("async def list_candidates", start)]

    # Scope-limited, by containment rather than by string prefix.
    assert "<<=" in body, "addresses must be tested for containment in the subnets"
    assert "scope" in body, "the swept subnets come from the run's recorded scope"
    # A run with no recorded scope cannot say what it covered.
    assert "if not subnets:" in body
    # Silence is measured against when this run STARTED, because the upsert stamps
    # last_seen = now() on everything the run saw.
    assert "last_seen <" in body
    # Only open candidates: an ignored or promoted one is not a finding to retract.
    assert "status = 'new'" in body
    # Marked, not deleted. The SQL keyword, not the word: the docstring says
    # "Marked, not deleted", and matching that made this fail on its own explanation.
    assert "DELETE FROM" not in body.upper(), (
        "a responder that used to answer is history worth keeping")


def test_a_responder_that_comes_back_is_resurrected_not_duplicated():
    """The index is what makes marking safe.

    It was UNIQUE on (address, protocol) WHERE status = 'new'. Once a row went to
    'gone' the upsert's ON CONFLICT no longer saw it, so a device that answered again
    would INSERT a second row for the same address - turning an aged-out responder
    into a duplicate the moment it came back. Both states share the index now, and the
    upsert flips 'gone' back to 'new'.
    """
    start = REPO.index("ON CONFLICT (address, protocol)")
    clause = REPO[start:REPO.index("RETURNING", start)]
    assert "status IN ('new', 'gone')" in clause, "the conflict target must span both"
    assert "status = 'new'" in clause, "answering again must undo having gone quiet"

    mig = next((Path(__file__).resolve().parents[1] / "alembic" / "versions")
               .glob("0079_*.py"))
    sql = mig.read_text(encoding="utf-8")
    assert "status IN ('new', 'gone')" in sql
    assert "uq_discovery_candidate_open" in sql


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
