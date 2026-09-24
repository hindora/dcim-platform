"""The commissioning queue proposes, and never applies.

The arithmetic is proved against a real database elsewhere. What is guarded here
is the pair of design rules a later change would quietly undo, because both look
like obvious improvements from inside the diff:

  1. It must not advance a lifecycle itself. An OS agent answering is not
     evidence that anybody ACCEPTED the machine - acceptance is a cut-over with a
     change record and a workload owner, and a poller cannot know that happened.
     Auto-advancing would also un-shelve a commissioning machine's alarms at
     exactly the moment its soak was most likely to be failing.

  2. Every state it offers must be a move the matrix allows, or the UI puts a 409
     behind a button and the operator is told "no" by a screen that offered the
     action in the first place.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.repositories.commissioning import DEFAULT_SOAK_HOURS, PROPOSED
from app.repositories.lifecycle import TRANSITIONS

APP = Path(__file__).resolve().parents[1] / "app"
REPO = (APP / "repositories" / "commissioning.py").read_text(encoding="utf-8")
API = (APP / "api" / "v1" / "commissioning.py").read_text(encoding="utf-8")
FRONTEND = (Path(__file__).resolve().parents[2] / "frontend" / "src"
            / "features" / "assets" / "commissioning" / "ReadyQueue.tsx")


def _sql(src: str) -> str:
    """Every SQL string in a module, comments stripped - so a scan reads what the
    query does rather than the prose explaining it."""
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(re.sub(r"--.*", "", node.value))
    return "\n".join(out)


# --------------------------------------------------------------- it proposes

@pytest.mark.parametrize("signal", sorted(PROPOSED))
def test_every_proposal_is_a_legal_transition(signal):
    """A queue offering a move the matrix refuses is a button that cannot work."""
    to_state = PROPOSED[signal]
    if to_state is None:
        return
    sources = [f for f, tos in TRANSITIONS.items() if to_state in tos]
    assert sources, f"{signal!r} proposes {to_state!r}, which nothing may reach"


def test_the_racked_signal_is_reachable_from_both_states_it_fires_on():
    """It fires on `planned` and `in_stock`, so both have to be able to get to
    `installed` - a queue that only worked for one of them would silently skip
    every device that had been received before it was racked."""
    for state in ("planned", "in_stock"):
        assert "installed" in TRANSITIONS[state]


def test_a_discrepancy_proposes_nothing():
    """A device answering while the record says it is gone has no safe button.
    Either it was never pulled or it was re-racked unrecorded, and which of those
    is true decides what should happen - so somebody has to go and look."""
    assert PROPOSED["discrepancy"] is None


# ------------------------------------------------------------ and never applies

def test_the_repository_only_reads():
    """One writer for lifecycle, and it is `lifecycle.record_transition`.

    A second path here would be a second set of rules - the matrix check, the
    event, the audit row and the alarm re-sync - to keep in step, and the first
    thing to drift would be the one nobody notices missing.
    """
    sql = _sql(REPO).upper()
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "MERGE "):
        assert verb not in sql, f"the queue repository issues {verb.strip()}"


def test_the_api_is_read_only():
    """No writes at all, so there is no way to confirm a row except through the
    ordinary transition endpoint - which is what keeps the audit trail honest and
    the matrix the only judge of a legal move."""
    tree = ast.parse(API)
    verbs = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(getattr(n.func, "value", None), ast.Name)
             and n.func.value.id == "router"}

    assert verbs == {"get"}, f"the queue router exposes {verbs}"
    assert "record_transition" not in API


def test_the_queue_never_calls_the_device_plane():
    """The DCIM must not know what is generating its telemetry.

    This router briefly carried a sync that logged into the simulator's REST API
    and read its topology export - one product reading another's database through
    its front door. Finding hardware is discovery's job, over the management
    network, and the importer is a fixture loader that belongs on a command line.
    """
    for bad in ("sim_import", "simulator_base_url", "fetch_topology",
                "TopologyImporter"):
        assert bad not in API, f"the queue router reaches for {bad}"


def test_the_ui_confirms_through_the_shared_transition_endpoint():
    """The button must post to /devices/{id}/lifecycle, not to a shortcut.

    That is what makes a confirmation from this screen indistinguishable in the
    history from one made on the asset's own Lifecycle tab - same event, same
    audit row, same matrix check.
    """
    src = FRONTEND.read_text(encoding="utf-8")

    assert "api.lifecycleTransition(" in src
    assert "reason:" in src, "a confirmation records WHY, which is the evidence"


def test_the_ui_offers_no_button_without_a_proposal():
    """A discrepancy row must render an investigate link, not a disabled or
    guessing action."""
    src = FRONTEND.read_text(encoding="utf-8")

    assert "row.proposed_state ?" in src
    assert "Investigate" in src


# ------------------------------------------------------------------- the soak

def test_the_soak_is_a_default_not_a_constant():
    """Burn-in length is local policy - 24-48h is usual - and an operator chasing
    one machine needs to see it before it has finished."""
    assert DEFAULT_SOAK_HOURS == 48
    assert "soak_hours" in API
    assert "soak_hours: int = DEFAULT_SOAK_HOURS" in REPO


def test_the_soak_clock_is_the_event_log_not_telemetry():
    """A soak starts when the machine was racked, which is a business event.

    Measuring it from a polling timestamp would restart the clock every time the
    collector reconnected, so a machine that flapped once would never become
    eligible.
    """
    sql = _sql(REPO)

    assert "device_lifecycle_event" in sql
    assert "last_success" in sql, "the endpoint timestamp is still shown as evidence"
    # But it is not what gates acceptance.
    gate = sql[sql.index("'ready'") - 600:sql.index("'ready'")]
    assert "soak_seconds" in gate


def test_a_server_needs_its_os_agent_and_a_switch_does_not():
    """A switch's agent is part of its NOS - there is no separate OS to deploy,
    so requiring an `os_agent` role would leave every switch stuck in
    `installed` for ever."""
    sql = _sql(REPO)

    assert "a.os_up OR (d.device_type <> 'server' AND a.any_up)" in sql


def test_disabled_endpoints_are_not_evidence():
    """Nobody is polling them, so their silence means nothing - and their last
    known state is whatever it was when somebody turned them off."""
    sql = _sql(REPO)

    assert "e.enabled" in sql
    assert "e.admin_state = 'enabled'" in sql



# ----------------------------------------------------- how hardware is found

DISCOVERY_SVC = (APP / "services" / "discovery.py").read_text(encoding="utf-8")
SWEEP_UI = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "features"
            / "assets" / "discovery" / "SweepPanel.tsx").read_text(encoding="utf-8")


def test_a_promoted_candidate_is_installed_not_in_service():
    """A sweep found a box answering on the management network. That is evidence
    somebody RACKED it and nothing more.

    Landing on `in_service` skipped the commissioning queue entirely - the device
    arrived already live and already paging, with nobody having looked at it.
    Acceptance is a cut-over with a change record and a workload owner, and
    promoting a candidate is not that decision.
    """
    body = DISCOVERY_SVC[DISCOVERY_SVC.index("async def promote("):
                         DISCOVERY_SVC.index("async def ignore(")]

    assert "'installed'," in body
    assert "'in_service'" not in body


def test_promotion_writes_the_first_lifecycle_event():
    """So the device has a history from the moment it enters inventory, and so the
    commissioning queue's soak clock has something to measure from. Without it the
    clock falls back to `updated_at`, which any later edit would reset."""
    body = DISCOVERY_SVC[DISCOVERY_SVC.index("async def promote("):
                         DISCOVERY_SVC.index("async def ignore(")]

    assert "INSERT INTO device_lifecycle_event" in body
    assert "to_state, reason, actor" in body or "'installed', :reason, :actor" in body


def test_promotion_does_not_invent_placement():
    """A sweep cannot know which rack a box is in, and a guessed U would put a
    wrong row in the elevation. The reservation carries placement; discovery does
    not."""
    body = DISCOVERY_SVC[DISCOVERY_SVC.index("async def promote("):
                         DISCOVERY_SVC.index("async def ignore(")]

    for column in ("rack_id", "u_start", "room_id"):
        assert column not in body, f"promotion is guessing {column}"


def test_the_sweep_is_queued_for_the_collector_not_run_by_the_api():
    """The sweep happens on the management network, which is where the collector
    is and the API is not. The UI queues a run; nothing in the browser or the API
    process touches a device."""
    assert "startDiscoveryRun" in SWEEP_UI
    assert "/discovery/runs" not in SWEEP_UI, "the UI goes through the client, not a raw URL"
    # And it never reaches for the other product.
    for bad in ("8001", "simulator", "topology/export"):
        assert bad not in SWEEP_UI.lower()


def test_the_subnets_are_suggested_from_inventory():
    """An operator should not have to know the site's addressing by heart to run
    an audit, and a free-text box invites both typos and a /16 - which the sweeper
    refuses outright rather than truncating, so the run just fails."""
    assert "discoverySubnets" in SWEEP_UI
    repo_src = (APP / "repositories" / "discovery.py").read_text(encoding="utf-8")
    assert "async def mgmt_subnets" in repo_src
    # The count is what makes a result readable.
    assert "count(*) AS known" in repo_src


# ------------------------------------------- a re-addressed device is not new

DISCOVERY_REPO = (APP / "repositories" / "discovery.py").read_text(encoding="utf-8")


def test_matching_prefers_the_serial_over_the_address():
    """The serial is the only key that survives a device being re-addressed.

    Matching on the address alone reported a moved machine as brand new, and
    promoting it created a SECOND record for one physical box - the exact failure
    an audit exists to catch, produced by the audit.
    """
    body = DISCOVERY_SVC[DISCOVERY_SVC.index("async def record_results("):]

    assert "match_serials" in body
    # Serial consulted FIRST, address only as the fallback.
    by_serial = body.index("by_serial.get(serial)")
    by_addr = body.index("known.get(addr)")
    assert by_serial < by_addr, "the address is being tried before the serial"


def test_a_serial_is_normalised_before_it_is_matched():
    """Gear reports its own serial inconsistently. A match that fails on a
    trailing space is worse than no match, because it looks like a new device."""
    from app.services.discovery import _serial_of

    assert _serial_of({"identity": {"serial": " abc123 "}}) == "ABC123"
    assert _serial_of({"identity": {"serial": ""}}) is None
    assert _serial_of({"identity": {}}) is None
    assert _serial_of({}) is None
    # Not a string: a device answering with a number must not crash a sweep.
    assert _serial_of({"identity": {"serial": 7}}) is None


def test_a_moved_device_is_counted_as_moved():
    """A device matched by serial at an address inventory did not expect has moved
    and nobody recorded it. That deserves its own number, not silence."""
    body = DISCOVERY_SVC[DISCOVERY_SVC.index("async def record_results("):]

    assert "readdressed" in body
    assert "known_address" in DISCOVERY_REPO


def test_the_candidate_says_why_it_matched():
    """"This is SW07" on a responder at a different address is a confusing line.
    "This is SW07, last known at 10.51.11.9" is an actionable one."""
    assert "matched_on_serial" in DISCOVERY_REPO
    assert "matched_device_address" in DISCOVERY_REPO


def test_discovery_no_longer_hard_wires_the_simulator_convention():
    """`PerAddressCommunity` is snmpsim's routing trick - one process serving every
    agent from one socket - and it was compiled into the collector's production
    path, so discovery could only ever have worked against that simulator.

    It is a legitimate thing to configure and a wrong thing to default to.
    """
    app_go = (Path(__file__).resolve().parents[2] / "collector" / "internal"
              / "app" / "app.go").read_text(encoding="utf-8")

    assert "discovery.PerAddressCommunity," not in app_go, (
        "the simulator's community convention is compiled in again")
    assert "a.discoveryCommunities()" in app_go

    cfg = (Path(__file__).resolve().parents[2] / "collector" / "internal"
           / "config" / "config.go").read_text(encoding="utf-8")
    assert "CommunityIsAddress" in cfg
    assert "Communities []string" in cfg
