"""Shelving, the lifecycle matrix, and the rule that is easy to half-apply.

The behaviour these protect is proved end to end against a real database in
tools; what is protected HERE is the part that rots quietly. Shelving is not one
predicate in one query - it is the same predicate in six, and the seventh gets
written by somebody who has never read migration 0046. That is what
`test_every_open_alarm_query_excludes_shelved` is for.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.repositories.lifecycle import TRANSITIONS, IllegalTransitionError

REPOS = Path(__file__).resolve().parents[1] / "app" / "repositories"
MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


# ------------------------------------------------------------------ shelving

def test_every_open_alarm_query_excludes_shelved():
    """One predicate, six queries, and no way to add a seventh without noticing.

    A query that reads open alarms and forgets the shelve clause puts planned
    work back on the console - or worse, leaves a room red while the alarm list
    that explains it is empty, which reads as a platform bug and burns the
    operator's trust in both.

    A query that genuinely needs shelved rows says so with a `shelve-exempt`
    comment, so the exemption is a decision somebody wrote down rather than an
    omission nobody noticed.
    """
    missed = []
    for path in sorted(REPOS.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        for match in re.finditer(r"WHERE a\.state <> 'CLEARED'", src):
            # The predicate may continue onto following lines.
            window = src[max(0, match.start() - 400):match.start() + 400]
            if "shelved_by_window" in window or "shelve-exempt" in window:
                continue
            missed.append(f"{path.name}:{src[:match.start()].count(chr(10)) + 1}")
    assert not missed, (
        f"open-alarm queries missing the shelve clause: {missed}. "
        "Add `AND a.shelved_by_window IS NULL`, or a `shelve-exempt` comment "
        "saying why this one wants them.")


def test_the_roll_up_excludes_shelved_but_not_symptoms():
    """The two suppressions are not the same rule, and folding them is a bug.

    A symptom is a genuine fault on a real device - it is held out of the LIST
    so one failure does not print twenty rows, but it still colours the device,
    because the device really is in trouble. A shelved alarm is the expected
    consequence of planned work and must not colour anything.

    Getting this backwards makes a room go red for a filter change, which is
    exactly the noise the feature exists to remove.
    """
    src = (REPOS / "alarms.py").read_text(encoding="utf-8")
    start = src.index("async def refresh_device_alarm_state")
    body = src[start:src.index("# ---", start)]

    assert "a.shelved_by_window IS NULL" in body
    assert "is_symptom" not in body


def test_shelving_is_stamped_at_raise_time():
    """Six detectors raise alarms and a seventh will be written later.

    Stamping inside the INSERT rather than at each call site is what makes the
    rule impossible to forget, so it is worth a test that says so.
    """
    src = (REPOS / "alarms.py").read_text(encoding="utf-8")
    insert = src[src.index("INSERT INTO alarm ("):]
    insert = insert[:insert.index("ON CONFLICT")]

    assert "shelved_by_window" in insert
    assert "maintenance_window w" in insert
    assert "w.status = 'active'" in insert
    assert "w.suppress" in insert


def test_touching_an_alarm_does_not_unshelve_it():
    """The ON CONFLICT branch must leave the mark alone.

    An alarm re-raised on every poll would otherwise un-shelve itself the first
    time the condition repeated, which is within seconds. Un-marking belongs to
    the window ending, and nowhere else.
    """
    src = (REPOS / "alarms.py").read_text(encoding="utf-8")
    conflict = src[src.index("ON CONFLICT (device_id, alarm_type, instance)"):]
    conflict = conflict[:conflict.index("RETURNING")]

    assert "shelved_by_window" not in conflict


def test_only_open_alarms_are_unshelved():
    """An alarm that cleared during the work stays marked.

    Un-marking it pushes it into the active list as freshly-visible history: a
    console that, the moment a window closes, fills with things that already
    broke and already recovered. What an operator wants after a window is what
    is wrong NOW.
    """
    src = (REPOS / "maintenance.py").read_text(encoding="utf-8")
    body = src[src.index("async def unshelve"):]
    body = body[:body.index("async def shelved_alarms")]

    assert "a.state <> 'CLEARED'" in body


def test_window_status_is_a_column_not_a_clock_comparison():
    """Two processes must not each decide whether a window is running.

    The API and the ingest worker both ask "is this device in a window". If that
    were `now() BETWEEN starts_at AND ends_at` evaluated in each, they would
    disagree at the boundary - and the disagreement shelves an alarm the
    operator can still see, or pages for one they cannot.
    """
    # Parsed, not sliced: the docstring above this very query explains the rule
    # using the words `now()`, and a scan over raw text fails on the
    # explanation rather than on the code. A test a comment can break is not
    # testing anything.
    tree = ast.parse((REPOS / "maintenance.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "active_window_for")
    # Skip the docstring NODE rather than subtracting its text: get_docstring()
    # returns a dedented copy that no longer matches the source constant, so a
    # string replace silently removes nothing.
    body = fn.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    sql = " ".join(n.value for stmt in body for n in ast.walk(stmt)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str))

    assert "w.status = 'active'" in sql
    assert "now()" not in sql


# ----------------------------------------------------------------- lifecycle

def test_every_state_can_be_reached_and_only_retired_is_terminal():
    reachable = {to for froms in TRANSITIONS.values() for to in froms}
    assert reachable | {"planned"} == set(TRANSITIONS)
    assert TRANSITIONS["retired"] == ()
    assert all(TRANSITIONS[s] for s in TRANSITIONS if s != "retired")


def test_a_decommissioned_device_can_return_to_the_spares_shelf():
    """The ordinary path, and forbidding it makes people create duplicates.

    A machine pulled from service and kept as a spare is one asset with one
    serial. If the model refuses the move, somebody records a second one.
    """
    assert "in_stock" in TRANSITIONS["decommissioned"]


def test_in_service_cannot_jump_straight_to_stock():
    """Hardware in a rack is not on a shelf. It has to be decommissioned first,
    which is the step that records that somebody pulled it."""
    assert "in_stock" not in TRANSITIONS["in_service"]


def test_a_refusal_says_what_is_allowed():
    """An operator told "no" needs to be told what IS possible; the matrix is
    the only thing that knows."""
    exc = IllegalTransitionError("retired", "in_service")

    assert exc.current == "retired"
    assert "terminal" in str(exc)

    exc = IllegalTransitionError("in_service", "in_stock")
    assert set(exc.allowed) == {"maintenance", "decommissioned"}
    assert "maintenance" in str(exc)


@pytest.mark.parametrize("state", sorted(TRANSITIONS))
def test_no_state_transitions_to_itself(state):
    assert state not in TRANSITIONS[state]


# ----------------------------------------------------------------- migration

def test_shelving_column_is_a_reference_not_a_boolean():
    """"3 alarms shelved" on a window page is how somebody notices the window
    was scoped too widely. A boolean cannot say by which window."""
    body = (MIGRATIONS / "0046_planned_work_should_not_page_anyone.py").read_text(
        encoding="utf-8")
    assert "shelved_by_window" in body
    assert 'sa.ForeignKey("maintenance_window.id"' in body


def test_phase_three_migrations_roll_back():
    for name in ("0045_a_lifecycle_needs_a_history.py",
                 "0046_planned_work_should_not_page_anyone.py"):
        tree = ast.parse((MIGRATIONS / name).read_text(encoding="utf-8"))
        downgrade = next(n for n in ast.walk(tree)
                         if isinstance(n, ast.FunctionDef) and n.name == "downgrade")
        assert not [n for n in ast.walk(downgrade) if isinstance(n, ast.Raise)], name
