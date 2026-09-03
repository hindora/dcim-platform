"""Bulk operations: the contract, not the plumbing.

Three properties make bulk safe to hand an operator, and all three are easy to
undo by accident later. The arithmetic is proved against a real database
elsewhere; these guard the shape.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.repositories.lifecycle import IllegalTransitionError
from app.repositories.parts import InsufficientStockError
from app.repositories.reservations import ReservationConflictError
from app.services import bulk

APP = Path(__file__).resolve().parents[1] / "app"
API = (APP / "api" / "v1" / "bulk.py").read_text(encoding="utf-8")
SERVICE = (APP / "services" / "bulk.py").read_text(encoding="utf-8")


# ------------------------------------------------------ per-row transactions

def test_rows_are_applied_in_their_own_savepoints():
    """A failure on row 3 must keep rows 1 and 2.

    One transaction for the batch discards work that succeeded, which for a
    retag of four hundred assets means doing all four hundred again because one
    was wrong.
    """
    body = SERVICE[SERVICE.index("async def run("):]
    per_row = body[body.index("for item in items:"):]
    assert "session.begin_nested()" in per_row


def test_atomic_is_available_and_is_not_the_default():
    """Moving half a rack is sometimes worse than moving none of it - but that
    is the exception, and defaulting to it would silently discard good work."""
    tree = ast.parse(SERVICE)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "run")
    kwonly = {a.arg: d for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=True)}
    assert "atomic" in kwonly
    assert kwonly["atomic"].value is False


# ----------------------------------------------------------- the report

def test_a_failure_names_the_object_and_carries_a_stable_key():
    """"2 failed" in a toast is how a feature becomes distrusted: the operator
    cannot tell which two, cannot retry them, and cannot find out why."""
    report = bulk.BulkReport()
    report.failed.append(bulk.RowResult(
        device_id="abc", name="SRV-02", error="rack_unit_occupied",
        message="U5 is occupied by SRV-BLOCKER"))
    out = report.as_dict()

    assert out["failed"][0]["name"] == "SRV-02"
    assert out["failed"][0]["error"] == "rack_unit_occupied"
    assert "SRV-BLOCKER" in out["failed"][0]["message"]


@pytest.mark.parametrize("exc,key", [
    (IllegalTransitionError("in_service", "in_stock"), "illegal_transition"),
    (InsufficientStockError("750W", 9, 2), "insufficient_stock"),
    (ReservationConflictError("U5 is occupied"), "reservation_conflict"),
])
def test_application_refusals_keep_their_own_key(exc, key):
    """Matched by TYPE, before the substring table.

    These are expected outcomes with a stable key of their own. Falling through
    to the string matcher reported them as `rejected` and logged them as
    surprises - which is what happened the first time this ran.
    """
    got_key, message = bulk.translate(exc)
    assert got_key == key
    # And they already carry a sentence written for a person.
    assert message == str(exc)


@pytest.mark.parametrize("text,key", [
    ('violates constraint "device_u_no_overlap"', "rack_unit_occupied"),
    ("duplicate key ... ix_device_mgmt_ip_live", "mgmt_ip_in_use"),
    ("duplicate key ... ix_device_serial_unique", "serial_in_use"),
    ("duplicate key ... ix_device_asset_tag_unique", "asset_tag_in_use"),
    ("violates foreign key constraint", "referenced"),
])
def test_every_constraint_the_bulk_paths_hit_is_translated(text, key):
    """Otherwise the feature returns "duplicate key value violates unique
    constraint ix_device_mgmt_ip_live" to a facilities technician."""
    assert bulk.translate(Exception(text))[0] == key


def test_an_unknown_failure_says_so_rather_than_guessing():
    """A wrong explanation is worse than an honest "we do not know"."""
    key, message = bulk.translate(Exception("something nobody predicted"))
    assert key == "rejected"
    assert "refused" in message


# --------------------------------------------------------------- auditing

def test_lifecycle_bulk_goes_through_the_single_device_service():
    """Sharing the service is what guarantees the bulk path cannot drift from
    the transition matrix, and that each device gets its own lifecycle event
    AND its own audit row - forty devices, forty of each."""
    body = API[API.index("async def bulk_lifecycle"):]
    body = body[:body.index("@router.post(\"/tags\"")]
    assert "lifecycle_service.transition" in body


@pytest.mark.parametrize("endpoint", ["bulk_fields", "bulk_move"])
def test_bulk_writes_one_audit_row_per_device(endpoint):
    """Per device, inside the per-row apply - not one summary row per batch.

    Parsed rather than grepped: one endpoint binds the id to a local first and
    the other passes it inline, and both are per-item. What matters is that the
    call is INSIDE apply and its target is not a constant.
    """
    tree = ast.parse(API)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == endpoint)
    apply_fn = next(n for n in ast.walk(fn)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "apply")

    calls = [n for n in ast.walk(apply_fn)
             if isinstance(n, ast.Call) and ast.unparse(n.func) == "audit.record"]
    assert len(calls) == 1, f"{endpoint}: expected one audit row per device"

    target = next(k.value for k in calls[0].keywords if k.arg == "target_id")
    assert not isinstance(target, ast.Constant), (
        f"{endpoint}: the audit row must name the device, not a fixed value")


# ------------------------------------------------------------------- scope

def test_placement_and_state_are_not_bulk_editable_fields():
    """They have endpoints of their own that reason about rack units and about
    the transition matrix. Letting /fields set them would route around both."""
    from app.api.v1.bulk import EDITABLE

    for forbidden in ("rack_id", "u_start", "lifecycle", "serial_number",
                      "mgmt_ip", "device_type"):
        assert forbidden not in EDITABLE, forbidden


def test_the_importer_may_set_an_asset_tag_but_never_a_serial():
    """A tag is a sticker somebody applied; a serial is what the hardware
    reports. A CSV that could overwrite serials would let a spreadsheet
    contradict the machine."""
    body = API[API.index("async def import_csv"):]
    assert 'fields["asset_tag"]' in body
    assert 'fields["serial_number"]' not in body


# ----------------------------------------------------------------- import

def test_import_is_always_two_phase():
    """An import that finds two bad rows in four hundred at write time has
    already written three hundred and ninety-eight, and nobody can tell which."""
    body = API[API.index("async def import_csv"):]
    assert 'mode not in ("validate", "apply")' in body
    assert 'if mode == "validate":' in body


def test_apply_refuses_a_file_that_changed_since_validation():
    """Stateless and tamper-evident. A server-side job would instead expire
    under somebody reviewing a long report."""
    body = API[API.index("async def import_csv"):]
    assert "digest_mismatch" in body
    assert "hashlib.sha256" in body


def test_the_dry_run_says_which_key_matched_each_row():
    """So an operator can see a row landing on a device by NAME when they
    expected it to match on serial."""
    from app.api.v1.bulk import MATCH_KEYS

    assert MATCH_KEYS == ("external_id", "serial_number", "asset_tag", "name")
    assert '"matched_by"' in API


def test_an_ambiguous_match_is_a_failure_not_a_guess():
    """Two devices with the same name is exactly when picking one is worst."""
    body = API[API.index("async def _match"):]
    assert "len(found) > 1" in body
    assert "ambiguous" in API


def test_validate_writes_nothing():
    """The whole point of the first phase."""
    body = API[API.index("async def import_csv"):]
    validate = body[body.index('if mode == "validate":'):]
    validate = validate[:validate.index("async def apply")]
    assert "UPDATE" not in validate
    assert "session.commit" not in validate


def test_bulk_batches_are_bounded():
    """An unbounded list is a request that holds a connection open for minutes
    and a report nobody can read."""
    for name in ("BulkLifecycle", "BulkTags", "BulkFields", "BulkMove"):
        block = API[API.index(f"class {name}("):]
        block = block[:block.index("\n\n\n")] if "\n\n\n" in block else block
        assert re.search(r"max_length=\d+", block), name
