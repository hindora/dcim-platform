"""Who owns a device's lifecycle state, and the two ways it was being taken away.

Both failures here were silent and both pointed the same direction - toward
`in_service`, the one state that pages - so the symptom was always a console
full of alarms for machines nobody had accepted yet, hours after somebody had
correctly marked them as still being built.

  1. The importer wrote `lifecycle = 'in_service'` on every conflict, so a
     re-import undid every operator decision on the estate.
  2. Nothing outside `staleness.py` read `lifecycle` at all, so `installed` -
     added by migration 0043 precisely so a machine mid-commissioning would not
     page - did nothing whatsoever.

Proved against a real database elsewhere. What is guarded here is the part that
rots quietly: a later edit restoring either unconditional write would pass every
other test in this suite.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.importer.simulator import TopologyImporter
from app.repositories.alarms import (
    NOT_SHELVED,
    SHELVE_REASON_NOT_COMMISSIONED,
    SHELVE_REASONS,
    SHELVED_LIFECYCLES,
)
from app.repositories.lifecycle import TRANSITIONS

APP = Path(__file__).resolve().parents[1] / "app"
MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
IMPORTER = (APP / "importer" / "simulator.py").read_text(encoding="utf-8")
LIFECYCLE = (APP / "repositories" / "lifecycle.py").read_text(encoding="utf-8")
ALARMS = (APP / "repositories" / "alarms.py").read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """Strip comments, so a scan reads what the code does rather than the prose
    explaining the very rule it enforces."""
    return re.sub(r"#.*", "", re.sub(r"--.*", "", text))


def _upsert() -> str:
    body = IMPORTER[IMPORTER.index("ON CONFLICT (external_id) DO UPDATE SET"):]
    return _code_only(body[:body.index("RETURNING id::text")])


# ------------------------------------------------------------------ importer

def test_the_upsert_has_no_opinion_about_lifecycle_at_all():
    """Presence in an export is evidence the hardware exists and nothing else.

    The simulator has no lifecycle field, so the importer has nothing to carry.
    Writing one anyway made the DCIM's own record the least durable thing in it:
    a state set by a human survived until the next import, and always landed on
    `in_service`, the one state that pages.

    The importer does still move devices - see `_decommission_missing` and
    `_resurrect` - but as transitions that leave a record, not as a column
    assignment buried in an upsert that runs against every device in the estate.
    """
    upsert = _upsert()

    assert "lifecycle" not in upsert, (
        "the upsert is writing lifecycle again; it runs on every device on "
        "every import, so anything it writes there overrules every operator")
    assert "decommissioned_at" not in upsert


def test_both_importer_moves_go_through_the_recorded_transition():
    """One writer, or the history is fiction.

    `record_transition` moves the device, writes the event an operator reads,
    keeps commissioned_at/decommissioned_at in step and re-syncs alarm shelving.
    An importer doing its own UPDATE did the first of those and none of the rest,
    so a swept device silently kept its shelved alarms and appeared in no history
    at all.
    """
    for fn in ("_decommission_missing", "_resurrect"):
        body = _code_only(IMPORTER[IMPORTER.index(f"async def {fn}"):])
        body = body[:body.index("self.report")]
        assert "self._move(" in body, f"{fn} moves a device without recording it"
        assert "UPDATE device" not in body, (
            f"{fn} has grown a second writer; go through _move")

    move = _code_only(IMPORTER[IMPORTER.index("async def _move"):])
    move = move[:move.index("async def _decommission_missing")]
    assert "lifecycle_repo.record_transition(" in move
    # Both records, the same pair every operator transition writes.
    assert "audit.record(" in move
    assert 'action="device.lifecycle"' in move


def test_the_importer_actor_is_distinct_from_the_0045_backfill():
    """`_resurrect` tells them apart, so they cannot be one string.

    Migration 0045 backfilled history out of two timestamps under the actor
    `import` - rows nobody can attribute. An event this importer wrote is
    attributable, and only those may be undone by restoring their from_state.
    """
    assert TopologyImporter._ACTOR != TopologyImporter._LEGACY_ACTOR

    backfill = (MIGRATIONS / "0045_a_lifecycle_needs_a_history.py").read_text(
        encoding="utf-8")
    assert f"'{TopologyImporter._LEGACY_ACTOR}'" in backfill


def test_a_sweep_records_the_state_it_swept_from():
    """`from_state` is the whole reason the resurrect can stop guessing.

    A bulk UPDATE cannot produce it - PostgreSQL does not expose the old row to
    RETURNING - which is why the sweep reads first and moves one at a time.
    """
    body = _code_only(
        IMPORTER[IMPORTER.index("async def _decommission_missing"):])
    body = body[:body.index("async def _resurrect")]

    assert "SELECT id::text AS id, lifecycle::text AS lifecycle" in body
    assert 'from_state=row["lifecycle"]' in body
    assert 'to_state="decommissioned"' in body


def test_absence_only_decommissions_states_that_should_be_present():
    """`in_stock` means "absent from the floor". Sweeping it is reading the
    evidence backwards - and `in_stock -> decommissioned` is not even a legal
    move, so the old sweep put rows into states the matrix refuses an
    operator."""
    sweep = _code_only(
        IMPORTER[IMPORTER.index("async def _decommission_missing"):])
    sweep = sweep[:sweep.index("async def _resurrect")]

    assert "lifecycle <> 'decommissioned'" not in sweep
    assert "lifecycle::text = ANY(:sweepable)" in sweep

    assert set(TopologyImporter._SWEEPABLE) == {
        "installed", "in_service", "maintenance"}
    # And the states it leaves alone are exactly the ones absence is normal for.
    assert not set(TopologyImporter._SWEEPABLE) & {
        "planned", "in_stock", "retired", "decommissioned"}


@pytest.mark.parametrize("state", ("installed", "in_service", "maintenance"))
def test_every_swept_state_may_legally_be_decommissioned(state):
    """The sweep is not an operator move and does not consult the matrix, but it
    must not produce a transition the matrix would have called impossible -
    that is a history no one can reconcile against the rules they were given."""
    assert state in TopologyImporter._SWEEPABLE
    assert "decommissioned" in TRANSITIONS[state]


def test_a_person_s_decommission_is_never_undone_by_an_import():
    """The device is in the export and the record says somebody retired it.

    That is a discrepancy, and discovery exists to surface it. An importer that
    resolves it by itself - every run, silently, in favour of the export - is
    the version of this code that has to be argued with at 2am.
    """
    body = _code_only(IMPORTER[IMPORTER.index("async def _resurrect"):])

    assert "l.actor IN (:actor, :legacy)" in body
    assert "l.device_id IS NULL" in body


def test_a_resurrect_restores_the_state_it_was_swept_from():
    """`in_service` was a guess, and the wrong one for anything swept out of
    `installed` or `maintenance`: it came back as something that pages."""
    body = _code_only(IMPORTER[IMPORTER.index("async def _resurrect"):])
    case = body[body.index("CASE WHEN l.actor = :actor"):]
    case = case[:case.index("END")]

    assert "THEN l.from_state" in case
    # And the documented fallback for history nobody can attribute.
    assert "ELSE 'in_service'" in case


def test_the_lookup_does_not_depend_on_ordering_two_events_in_one_transaction():
    """`ts` defaults to `now()`, which is TRANSACTION start time.

    Two events for one device written in the same transaction therefore carry
    the same timestamp, and `ORDER BY ts DESC, id` tie-breaks on a random uuid -
    so "the newest event" is a coin toss. Asking specifically for the newest
    DECOMMISSION removes the question: a candidate is decommissioned right now,
    so that event is the one that put it there.
    """
    body = _code_only(IMPORTER[IMPORTER.index("async def _resurrect"):])
    cte = body[body.index("), decommission AS ("):body.index("SELECT c.id::text")]

    assert "WHERE e.to_state = 'decommissioned'" in cte
    # And where a tie is still possible - one device decommissioned twice in one
    # transaction - it resolves toward leaving the device alone.
    assert "(e.actor IN (:actor, :legacy)) ASC" in cte


def test_the_resurrect_scopes_its_history_lookup():
    """A DISTINCT ON over the whole event table to answer a question about the
    handful of devices that came back reads the estate's entire history on every
    import, and that table only grows."""
    body = _code_only(IMPORTER[IMPORTER.index("async def _resurrect"):])

    assert "JOIN cand c ON c.id = e.device_id" in body


def test_both_moves_run_over_the_same_set():
    """One `present` set, computed once. Two call sites each building their own
    is how the sweep and the resurrect come to disagree about which devices are
    in the export, and a device in neither set silently keeps a stale state."""
    run = IMPORTER[IMPORTER.index("async def run("):]
    run = run[:run.index("await self._upsert_terminations")]

    assert run.count("present = {d.get(\"id\")") == 1
    assert "await self._resurrect(present)" in run
    assert "await self._decommission_missing(present)" in run


# --------------------------------------------------------- installed shelving

def test_installed_is_the_state_that_shelves():
    """Migration 0043 said what `installed` is for in its first paragraph:
    racked and cabled, in the elevations and in the capacity figures, and paging
    nobody. The third part is this constant."""
    assert SHELVED_LIFECYCLES == ("installed",)
    assert "installed" in TRANSITIONS
    # It is a way-station, not somewhere a device is left: both the acceptance
    # path and the way back out have to exist or the state is a trap.
    assert "in_service" in TRANSITIONS["installed"]
    assert "in_stock" in TRANSITIONS["installed"]


def test_the_reason_is_a_shared_constant_not_a_string_in_two_files():
    """Three places write or read this value - the raise-time stamp, the
    transition either side, and the migration's CHECK. A literal in each is how
    one of them ends up spelled differently and shelves nothing."""
    assert SHELVE_REASON_NOT_COMMISSIONED in SHELVE_REASONS
    assert NOT_SHELVED == "a.shelved_reason IS NULL"

    migration = (MIGRATIONS
                 / "0075_a_machine_being_commissioned_must_not_page.py"
                 ).read_text(encoding="utf-8")
    for reason in SHELVE_REASONS:
        assert reason in migration


def test_a_transition_resyncs_what_was_already_standing():
    """The raise-time stamp only covers alarms raised AFTER the move.

    A machine pulled back for rework at 14:00 has alarms from 13:00 on it, and
    they are the loud ones. Without this the state change goes quiet only as
    each condition happens to re-raise, which for a dwell-based rule can be
    never.
    """
    body = _code_only(
        LIFECYCLE[LIFECYCLE.index("async def _resync_shelving"):])

    # Both directions.
    assert "SET shelved_reason = :reason" in body
    assert "SET shelved_reason = NULL" in body
    # Called from the transition, not left for a caller to remember.
    assert "_resync_shelving(" in LIFECYCLE[
        LIFECYCLE.index("async def record_transition"):
        LIFECYCLE.index("async def _resync_shelving")]


def test_releasing_only_touches_what_this_reason_holds():
    """A window running over the same device keeps its own mark.

    Clearing the column outright would un-shelve an alarm an engineer is
    standing in front of, which is the failure shelving exists to prevent -
    reached from the other direction.
    """
    body = _code_only(
        LIFECYCLE[LIFECYCLE.index("async def _resync_shelving"):])
    release = body[body.index("SET shelved_reason = NULL"):]

    assert "shelved_reason = :reason" in release


def test_a_transition_recomputes_the_roll_up():
    """Shelving without it leaves a red room over an empty alarm list, which
    reads as a platform bug. Same ordering rule as `services.maintenance`."""
    body = LIFECYCLE[LIFECYCLE.index("async def _resync_shelving"):]

    assert "refresh_device_alarm_state" in body


def test_a_cleared_alarm_is_not_resurrected_by_acceptance():
    """What broke and recovered while the machine was being built is history.

    Un-shelving it fills the console, at the moment somebody accepts a machine,
    with things that are no longer wrong - the same reason `maintenance.unshelve`
    only releases open rows.
    """
    body = _code_only(
        LIFECYCLE[LIFECYCLE.index("async def _resync_shelving"):])

    assert body.count("state <> 'CLEARED'") == 2


def test_the_stamp_reads_lifecycle_from_the_joined_device():
    """A platform alarm has no device, and `d.lifecycle` is then NULL.

    The CASE has to fall through to NULL rather than to a reason: shelving the
    alarms that say the monitoring itself is broken would hide the outage from
    the only list that could report it.
    """
    insert = ALARMS[ALARMS.index("INSERT INTO alarm ("):]
    insert = insert[:insert.index("ON CONFLICT")]

    assert "LEFT JOIN device d" in insert
    assert "WHEN d.lifecycle = 'installed'" in insert
    # No ELSE: an unmatched CASE is NULL, which is "not shelved".
    stamp = insert[insert.index("WHEN d.lifecycle = 'installed'"):]
    assert "ELSE" not in stamp[:stamp.index("END")]


# ------------------------------------------------------- the fresh-build trap

def test_no_migration_compares_a_later_label_as_a_bare_enum_literal():
    """A trap that only fires on a database built from scratch.

    `alembic/env.py` wraps the WHOLE upgrade in one transaction, so on a fresh
    build 0043's `ALTER TYPE lifecycle_t ADD VALUE` and every migration after it
    run together. PostgreSQL refuses to USE a new label in the transaction that
    added it - "unsafe use of new value" - so `WHERE lifecycle = 'installed'` in
    a later migration passes on every existing database and fails the moment CI
    builds one from nothing.

    `lifecycle::text = 'installed'` is an ordinary string comparison with no
    value to resolve, and is legal anywhere. This scan enforces that spelling,
    which is the only part of the rule a reviewer cannot see by reading a diff.
    """
    late = {"in_stock", "installed", "retired"}
    offenders = []
    for path in sorted(MIGRATIONS.glob("0*.py")):
        # 0043 declares the labels; it is the one file that may name them.
        if path.name.startswith("0043"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # The module docstring EXPLAINS this rule using the words it forbids, and
        # several migrations discuss the states in prose. Scanning raw text fails
        # on the explanation rather than on the code, so read the SQL only: every
        # string constant except the one the module opens with.
        body = tree.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        for stmt in body:
            for node in ast.walk(stmt):
                if not (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)):
                    continue
                for line in node.value.splitlines():
                    code = line.split("--")[0]
                    if "lifecycle" not in code or "::text" in code:
                        continue
                    if any(f"'{label}'" in code for label in late):
                        offenders.append(f"{path.name}: {code.strip()}")
    assert not offenders, (
        f"enum literal usable only after a commit: {offenders}. "
        "Compare `lifecycle::text` instead - see migration 0075.")
