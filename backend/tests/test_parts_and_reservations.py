"""Stock as a ledger, and capacity that is actually held.

The arithmetic is proved against a real database elsewhere. What is guarded here
is the pair of design rules that a later change would quietly undo: that nothing
sets a stock quantity directly, and that rack units are enforced by the
constraint that already works rather than by a second mechanism.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.repositories.parts import InsufficientStockError

APP = Path(__file__).resolve().parents[1] / "app"
MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
PARTS = (APP / "repositories" / "parts.py").read_text(encoding="utf-8")
RESERVATIONS = (APP / "repositories" / "reservations.py").read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """Strip comments, so a scan reads what the code does rather than the prose
    explaining the very rule it enforces."""
    return re.sub(r"#.*", "", re.sub(r"--.*", "", text))


# -------------------------------------------------------------- the ledger

def test_nothing_assigns_a_stock_quantity():
    """`on_hand` is only ever incremented by a movement.

    A figure somebody can overwrite cannot answer "we had four last week, where
    did they go", and every discrepancy becomes one person's memory against a
    number. Correcting a miscount is an `adjustment` with a note - the same
    operation under a name that leaves a record.
    """
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        for match in re.finditer(r"SET\s+on_hand\s*=\s*([^\n,]+)",
                                 _code_only(path.read_text(encoding="utf-8"))):
            expr = match.group(1).strip()
            # The one legal form: add a delta to what is already there.
            if expr.startswith("on_hand +"):
                continue
            offenders.append(f"{path.relative_to(APP)}: SET on_hand = {expr}")
    assert not offenders, offenders


def test_the_api_offers_no_way_to_set_a_quantity():
    """Not merely absent - there must be no endpoint shaped like one."""
    src = (APP / "api" / "v1" / "inventory.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fields = {t.id for node in ast.walk(tree)
              if isinstance(node, ast.ClassDef) and node.name.endswith("In")
              for stmt in node.body if isinstance(stmt, ast.AnnAssign)
              for t in [stmt.target] if isinstance(t, ast.Name)}

    assert "delta" in fields, "movements are how stock changes"
    assert "on_hand" not in fields
    assert "quantity" not in fields


def test_a_negative_delta_is_applied_by_update_not_upsert():
    """PostgreSQL checks the row an INSERT PROPOSES before it resolves
    ON CONFLICT.

    So `VALUES (..., -3)` trips `on_hand >= 0` even when the DO UPDATE branch
    would land on 7 - which is a constraint violation on a movement that is
    perfectly legal. The row is ensured at zero first and the delta added by an
    UPDATE, so the check runs once, on the value that actually results.
    """
    body = _code_only(PARTS[PARTS.index("async def move("):])
    body = body[:body.index("async def movements")]

    assert "ON CONFLICT (part_id, store_id) DO NOTHING" in body
    assert "SET on_hand = on_hand + :delta" in body
    assert "part_stock.on_hand + EXCLUDED.on_hand" not in body


def test_the_balance_is_locked_before_it_is_checked():
    """Two concurrent consumptions of the last unit must not both pass.

    The CHECK constraint would catch it anyway, but as a constraint violation
    rather than a sentence naming the part and the count - and that is not
    something to put in front of an operator.
    """
    body = PARTS[PARTS.index("async def move("):]
    assert "FOR UPDATE" in body[:body.index("INSERT INTO part_stock")]


def test_a_refusal_names_the_part_and_the_numbers():
    exc = InsufficientStockError("750W-PSU", 99, 7)
    assert "750W-PSU" in str(exc)
    assert "99" in str(exc) and "7" in str(exc)
    assert exc.wanted == 99 and exc.have == 7


def test_an_adjustment_must_say_why():
    """An adjustment overrides the ledger with a physical count. Without a
    reason it is the silent overwrite this table exists to prevent, wearing a
    different name."""
    body = (MIGRATIONS / "0048_stock_is_a_ledger_not_a_number.py").read_text(
        encoding="utf-8")
    assert "reason <> 'adjustment' OR note IS NOT NULL" in body


def test_consuming_parts_is_all_or_nothing():
    """A record claiming two PSUs went in while one left stock describes work
    that did not happen the way it is written down."""
    src = (APP / "services" / "parts.py").read_text(encoding="utf-8")
    body = src[src.index("async def consume_for_record"):]
    body = body[:body.index("def normalise_lines")]
    # No try/except swallowing a failed line - the caller's transaction rolls
    # the whole record back.
    assert "except" not in body


# --------------------------------------------------------- reservations

def test_rack_units_are_enforced_by_the_existing_constraint():
    """No second mechanism.

    Exclusion constraints cannot span tables, and a cross-table trigger doing
    the same job needs explicit locking to be correct - easy to get subtly
    wrong, impossible to notice until two installs collide. A reservation that
    names rack units inserts a `planned` device instead, and
    device_u_no_overlap refuses the overlap with no new code.
    """
    body = _code_only(RESERVATIONS)
    assert "device_u_no_overlap" in body
    assert "INSERT INTO device" in body
    assert "'planned'" in body
    assert "CREATE TRIGGER" not in body


def test_a_conflict_names_the_occupier_not_the_constraint():
    """An operator needs to know WHAT is in U20-24, not that a GiST exclusion
    constraint fired."""
    body = RESERVATIONS[RESERVATIONS.index("async def create("):]
    body = body[:body.index("async def release")]
    assert "occupant_of" in body
    assert "is occupied" in body


def test_releasing_takes_the_placeholder_with_it():
    """A `planned` device left holding rack units against a reservation that no
    longer exists is a row nobody can explain and nobody thinks to delete."""
    body = RESERVATIONS[RESERVATIONS.index("async def release("):]
    body = body[:body.index("async def fulfil")]
    assert "DELETE FROM device" in body
    assert "lifecycle = 'planned'" in body


def test_fulfilling_promotes_rather_than_recreates():
    """Delete-and-create would briefly vacate the very rack units the
    reservation was holding, which is when somebody else's install slips in."""
    body = RESERVATIONS[RESERVATIONS.index("async def fulfil("):]
    body = body[:body.index("async def expire_due")]
    assert "UPDATE device" in body
    assert "DELETE FROM device" not in body


def test_a_reservation_must_expire():
    """The failure mode of this feature everywhere it exists is a rack held for
    a project cancelled two years ago that nobody released."""
    body = (MIGRATIONS / "0049_hold_the_space_or_lose_it.py").read_text(
        encoding="utf-8")
    assert re.search(r'"expires_at", sa\.Date, nullable=False', body)


def test_a_u_range_without_a_rack_is_refused():
    """Rack units in no rack cannot be enforced by anything."""
    body = (MIGRATIONS / "0049_hold_the_space_or_lose_it.py").read_text(
        encoding="utf-8")
    assert "u_start IS NULL OR rack_id IS NOT NULL" in body


@pytest.mark.parametrize("name", ["0048_stock_is_a_ledger_not_a_number.py",
                                  "0049_hold_the_space_or_lose_it.py"])
def test_phase_five_migrations_roll_back(name):
    tree = ast.parse((MIGRATIONS / name).read_text(encoding="utf-8"))
    downgrade = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "downgrade")
    assert not [n for n in ast.walk(downgrade) if isinstance(n, ast.Raise)]
