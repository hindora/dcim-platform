"""Cover, and the cache that must never be stale.

`device.warranty_expires` is a denormalisation with exactly one legitimate
writer. The behaviour is proved against a real database elsewhere; what is
guarded here is the part that rots - a new code path that changes cover and
forgets to recompute, or a second writer appearing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.repositories import contracts as repo
from app.repositories import tags as tag_repo
from app.repositories.tags import OBJECT_TYPES, UnknownObjectTypeError

APP = Path(__file__).resolve().parents[1] / "app"
MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


# ------------------------------------------------------------------- the cache

def test_only_the_recompute_writes_warranty_expires():
    """One writer, or the cache disagrees with itself.

    It is read by a filter, a sort and a landing-page tile. A second place that
    sets it - an importer, a bulk edit, a well-meaning PATCH - produces a column
    that is right on the screen somebody last looked at and wrong on the others.

    Scanned as SQL writes rather than as the identifier: `warranty_expires`
    appears all over the read paths, and a test that cannot tell a SELECT from
    an UPDATE would fail on its own filters.
    """
    writers = []
    for path in sorted(APP.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        for match in re.finditer(r"SET\s+warranty_expires\s*=", src):
            writers.append(f"{path.relative_to(APP)}:"
                           f"{src[:match.start()].count(chr(10)) + 1}")

    assert len(writers) == 1, writers
    assert writers[0].startswith("repositories/contracts.py:"), writers


def test_cover_is_the_latest_end_date_not_the_earliest():
    """With cover to 2027 and to 2029 a device is covered until 2029.

    The earliest end date is when the FIRST contract lapses, which is a
    different question and not the one an asset list asks. This was written both
    ways in the spec before it was settled.
    """
    src = (APP / "repositories" / "contracts.py").read_text(encoding="utf-8")
    body = src[src.index("async def recompute_warranty"):]

    assert "MAX(c.end_date)" in body
    assert "MIN(c.end_date)" not in body


def test_a_contract_that_has_not_started_is_not_cover():
    """A contract signed for next quarter is not support today.

    Reporting it as cover is how a machine goes to site believing it has
    support it cannot yet claim.
    """
    src = (APP / "repositories" / "contracts.py").read_text(encoding="utf-8")
    body = src[src.index("async def recompute_warranty"):]

    assert "c.start_date <= CURRENT_DATE" in body


@pytest.mark.parametrize("func", ["create", "update", "delete", "cover", "uncover"])
def test_every_path_that_changes_cover_recomputes(func):
    """The reason the service layer exists at all.

    Adding a device, removing one, moving a contract's dates and deleting a
    contract all change what a device is covered until. A router calling the
    repository directly would skip the recompute, and nothing would look wrong
    until somebody sorted the asset list by expiry.
    """
    tree = ast.parse((APP / "services" / "contracts.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == func)
    calls = {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}

    assert "repo.recompute_warranty" in calls


def test_editing_a_note_does_not_rewrite_two_hundred_devices():
    """Only a date move changes cover.

    Renaming a reference or editing a note does not, and recomputing the whole
    covered set for it would be waste on every keystroke of an edit form.
    """
    src = (APP / "services" / "contracts.py").read_text(encoding="utf-8")
    body = src[src.index("async def update"):src.index("async def delete")]

    assert '{"start_date", "end_date"} & set(changes)' in body


def test_the_expiring_threshold_is_served_not_assumed():
    """The tile, the filter and the record must agree about "expiring".

    They would not, the first time somebody wrote 90 into a component - so the
    API hands the number out and the UI renders "expiring within {n} days" from
    what it was given.
    """
    assert repo.EXPIRING_DAYS == 90

    src = (APP / "api" / "v1" / "contracts.py").read_text(encoding="utf-8")
    assert '"expiring_days": service.EXPIRING_DAYS' in src

    # And the service re-exports it, so there is one constant behind all three.
    from app.services import contracts as svc
    assert svc.EXPIRING_DAYS is repo.EXPIRING_DAYS


# -------------------------------------------------------------------- tagging

def test_tag_targets_are_a_closed_list():
    """An unchecked object_type is a row pointing at nothing.

    There is no foreign key - the column is polymorphic, like connection
    terminations - so the repository is the only thing standing between a typo
    and an orphan.
    """
    assert OBJECT_TYPES == ("device", "rack", "room")


@pytest.mark.asyncio
async def test_tagging_something_that_cannot_be_tagged_is_refused():
    with pytest.raises(UnknownObjectTypeError) as exc:
        await tag_repo.tags_for(None, "datacenter", "x")
    assert "device" in str(exc.value)


@pytest.mark.asyncio
async def test_tags_are_fetched_for_the_whole_page_at_once():
    """200 rows must not become 200 round trips."""
    assert await tag_repo.tags_for_devices(None, []) == {}


def test_the_device_list_attaches_tags_in_one_query():
    src = (APP / "services" / "devices.py").read_text(encoding="utf-8")
    body = src[src.index("async def list_devices"):]
    body = body[:body.index("async def get_device")]

    assert "tags_for_devices" in body


# ------------------------------------------------------------------ migration

def test_a_supplier_cannot_reuse_a_reference():
    """Scoped to the supplier, because two vendors will both use "C-1001"."""
    body = (MIGRATIONS / "0047_cover_is_a_contract_not_a_date.py").read_text(
        encoding="utf-8")
    assert 'sa.UniqueConstraint("supplier_id", "reference"' in body


def test_dropping_the_contracts_clears_the_cache():
    """warranty_expires caches something that would no longer exist.

    Leaving the number behind after a downgrade means a column nothing can
    explain, which is worse than an empty one.
    """
    body = (MIGRATIONS / "0047_cover_is_a_contract_not_a_date.py").read_text(
        encoding="utf-8")
    downgrade = body[body.index("def downgrade"):]
    assert "UPDATE device SET warranty_expires = NULL" in downgrade
