"""Identity: the column reconciliation matches on, and the rules around it.

docs/19 B2 is the reason this file exists. `serial_number` and `asset_tag` sat on
`device` from the baseline, NULL on all 664 rows, with no unique index - so
discovery's matching, which tries serial first, could never match anything and
every sweep produced duplicates somebody resolved by hand.

What is worth testing is not that a column exists. It is the two rules that are
easy to write the obvious wrong way: the importer must never ERASE an identity,
and it must never OWN the one it has no business owning.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.repositories import assets as assets_repo
from app.services import assets as assets_service

IMPORTER = Path(__file__).resolve().parents[1] / "app" / "importer" / "simulator.py"
SOURCE = IMPORTER.read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """Strip comments so a scan reads what the code DOES, not what it says.

    Both of the scanning tests below first failed against prose explaining the
    very rule they enforce - the comment naming `asset_tag` as deliberately
    absent read as `asset_tag` being present. A test that a docstring can break
    is not testing the code.
    """
    without_sql = re.sub(r"--.*", "", text)
    return re.sub(r"#.*", "", without_sql)


# ------------------------------------------------------------------ importer

def test_importer_writes_the_serial():
    """Without this the estate stays unidentified however good the export is."""
    assert "serial_number" in SOURCE
    assert ":serial" in SOURCE


def test_a_reimport_never_erases_an_existing_serial():
    """COALESCE, not a plain assignment.

    An export taken before the simulator carried serials - or one where a device
    is temporarily unreadable - would otherwise blank the identity the whole
    estate reconciles on, and the next discovery sweep would report every device
    as new.
    """
    assert re.search(
        r"serial_number\s*=\s*COALESCE\(\s*EXCLUDED\.serial_number,\s*"
        r"device\.serial_number\s*\)", SOURCE)


def test_the_importer_does_not_own_asset_tags():
    """An asset tag is a sticker somebody put on a chassis.

    It is entered by facilities and has no representation upstream, so an
    importer that wrote the column would overwrite hand-entered data with
    nothing on every run.
    """
    upsert = SOURCE[SOURCE.index("INSERT INTO device"):]
    upsert = _code_only(upsert[:upsert.index("RETURNING id::text")])
    assert "asset_tag" not in upsert


# ------------------------------------------------------------------ vocabulary

def test_every_lifecycle_state_is_counted_even_at_zero():
    """A state with no devices reports 0 rather than vanishing.

    Absence and zero render identically to a careless UI, and the day the last
    decommissioned device is purged the landing page would quietly lose a
    column rather than show an empty one.
    """
    assert assets_repo.ALL_LIFECYCLES == (
        "planned", "in_stock", "installed", "in_service",
        "maintenance", "decommissioned", "retired")


def test_installed_counts_against_capacity_but_in_stock_does_not():
    """The distinction migration 0043 exists for.

    An installed machine is racked - it occupies U and it is part of the
    estate's placement. One in stock is on a shelf and occupies nothing. Folding
    them together makes a delivery look like consumed rack space.
    """
    assert "installed" in assets_repo.LIVE_LIFECYCLES
    assert "in_stock" not in assets_repo.LIVE_LIFECYCLES
    assert "planned" not in assets_repo.LIVE_LIFECYCLES


@pytest.mark.asyncio
async def test_filter_options_serve_all_seven_states():
    """The UI must never hard-code a state the database does not have, and must
    never be missing one it does."""

    class _Session:
        async def execute(self, *_a, **_k):
            class _R:
                def mappings(self):
                    return self

                def all(self):
                    return []
            return _R()

    out = await assets_service.filter_options(_Session())

    assert [life["value"] for life in out["lifecycles"]] == list(
        assets_repo.ALL_LIFECYCLES)
    assert all(life["label"] for life in out["lifecycles"])


# ------------------------------------------------------------------ migration

MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def test_enum_labels_land_in_a_migration_of_their_own():
    """PostgreSQL forbids USING a new enum label in the transaction that ADDED
    it, and alembic wraps each migration in one.

    A migration that adds `in_stock` and then updates a row to it fails with
    "unsafe use of new value" - so 0043 adds labels and does nothing else. This
    guards that, because the natural instinct on the next state added is to
    fold it into whichever migration needs it.
    """
    module = MIGRATIONS / "0043_lifecycle_gains_three_states.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))
    upgrade = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "upgrade")

    # What upgrade() actually calls, ignoring every word written about it.
    called = {ast.unparse(n.func) for n in ast.walk(upgrade)
              if isinstance(n, ast.Call)}
    assert called <= {"op.execute", "f"}, called
    assert "ALTER TYPE lifecycle_t ADD VALUE" in ast.unparse(upgrade)


def test_identity_indexes_are_partial():
    """Two assets may both be unidentified; no two may claim one identity."""
    body = (MIGRATIONS / "0044_an_asset_needs_an_identity.py").read_text(
        encoding="utf-8")
    for column in ("serial_number", "asset_tag"):
        assert re.search(
            rf"CREATE UNIQUE INDEX ix_device_{column.replace('serial_number', 'serial')}"
            rf"\w*_unique ON device \({column}\)\s*WHERE {column} IS NOT NULL",
            body), column


def test_every_migration_can_be_rolled_back():
    """The repo policy, enforced where it is cheap to check.

    The CI workflow says it in its own words - "a migration that cannot be
    rolled back is a migration you cannot deploy on a Friday" - and runs
    `alembic downgrade base` to prove it. This catches the same mistake before
    the push rather than after, which is how 0043 shipped with a downgrade that
    raised NotImplementedError and took the Migrations job down.
    """
    offenders = []
    for path in sorted(MIGRATIONS.glob("[0-9]*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        downgrade = next((n for n in ast.walk(tree)
                          if isinstance(n, ast.FunctionDef)
                          and n.name == "downgrade"), None)
        if downgrade is None:
            offenders.append(f"{path.name}: no downgrade()")
            continue
        raises = [n for n in ast.walk(downgrade) if isinstance(n, ast.Raise)]
        if raises:
            offenders.append(f"{path.name}: downgrade() raises")
    assert not offenders, offenders
