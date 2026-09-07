"""Scoping the alarm list to one room.

The home panel's rows say "this room has 4 alerts"; opening a row asks the
alarm list for that room's conditions and prints them. Those two numbers are
produced by different queries, so they only agree if the second one resolves a
room the same way the first one does:

* through the rack when a device is racked, off the device when it is not -
  a CRAH standing on the floor is in a room without being in a rack;
* and skipping decommissioned kit, which the roll-up already skips.

Get either wrong and the row and its expansion disagree in front of an
operator, which is worse than not being able to expand at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.repositories import alarms as repo

pytestmark = pytest.mark.asyncio


class _CapturingSession:
    """Records the statement instead of running it."""

    def __init__(self) -> None:
        self.sql: str = ""
        self.params: dict[str, Any] = {}

    async def execute(self, statement, params=None):
        self.sql = str(statement)
        self.params = params or {}
        return _EmptyResult()


class _EmptyResult:
    def mappings(self):
        return self

    def all(self):
        return []


async def _run(**kwargs) -> _CapturingSession:
    session = _CapturingSession()
    await repo.list_alarms(session, **kwargs)  # type: ignore[arg-type]
    return session


async def test_a_room_narrows_the_list():
    session = await _run(room_id="11111111-1111-1111-1111-111111111111")
    assert "rm.id = CAST(:room_id AS uuid)" in session.sql
    assert session.params["room_id"] == "11111111-1111-1111-1111-111111111111"


async def test_the_list_pages_by_offset_over_a_total_order():
    """A caller walking the list by offset must see every row exactly once.

    Two conditions raised in the same tick share a last_seen, so severity
    and last_seen alone leave the page boundary to chance; the id breaks the
    tie. The room expansion reads a hall of thousands this way.
    """
    session = await _run(limit=500, offset=1000)
    assert session.params["limit"] == 500
    assert session.params["offset"] == 1000
    assert "LIMIT :limit OFFSET :offset" in session.sql
    assert "a.last_seen DESC, a.id" in session.sql


async def test_the_room_is_resolved_through_the_rack_or_the_device():
    """The join the filter rides on, not the filter itself.

    A CRAH is in a room and in no rack. If this COALESCE ever becomes
    `rr.room_id`, every facility device silently leaves the expansion while
    still being counted on the row above it.
    """
    session = await _run(room_id="11111111-1111-1111-1111-111111111111")
    assert "COALESCE(rr.room_id, d.room_id)" in session.sql


async def test_a_scoped_list_skips_decommissioned_kit():
    """The roll-up counts live devices only; the expansion must match it."""
    session = await _run(room_id="11111111-1111-1111-1111-111111111111")
    assert "d.lifecycle <> 'decommissioned'" in session.sql


async def test_an_unscoped_list_is_unchanged():
    """No room asked for, no room predicate - and no lifecycle filter either.

    The device page and the alarm page both list alarms without a room, and a
    decommissioned device's open alarms are exactly what somebody chasing a
    cleanup wants to see.
    """
    session = await _run()
    # The bind parameter, not the word: `room_id` is in the SELECT's own
    # COALESCE and always will be.
    assert ":room_id" not in session.sql
    assert "lifecycle" not in session.sql


async def test_a_room_composes_with_the_category_filter():
    """The panel always sends both: this room, these domains."""
    session = await _run(room_id="11111111-1111-1111-1111-111111111111",
                         categories=["cooling", "power"])
    assert "rm.id = CAST(:room_id AS uuid)" in session.sql
    assert "a.category = ANY(:categories)" in session.sql
    assert session.params["categories"] == ["cooling", "power"]


async def test_the_default_still_hides_symptoms():
    """One root cause with twenty symptoms is one incident, expanded or not."""
    session = await _run(room_id="11111111-1111-1111-1111-111111111111")
    assert "NOT a.is_symptom" in session.sql
