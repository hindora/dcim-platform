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
    """No POST, so there is no way to confirm a row except through the ordinary
    transition endpoint - which is what keeps the audit trail honest."""
    tree = ast.parse(API)
    verbs = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(getattr(n.func, "value", None), ast.Name)
             and n.func.value.id == "router"}

    assert verbs == {"get"}, f"the queue router exposes {verbs}"


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
