"""The asset view: derived numbers, and the filter rules that are easy to get
subtly wrong.

The SQL itself either runs or it does not, and the shape of the summary is
proved by running it. What is worth a test is the arithmetic that would still
return a plausible number if it drifted, and the two filter rules where the
obvious implementation is wrong in a way nobody would notice from the UI.
"""

from __future__ import annotations

import pytest

from app.repositories import devices as device_repo
from app.services import assets as service


class _FakeSession:
    """Records the statement it was handed instead of executing it."""

    def __init__(self, rows: list[dict] | None = None):
        self.rows = rows or []
        self.sql: str = ""
        self.params: dict = {}

    async def execute(self, statement, params=None):
        self.sql = str(statement)
        self.params = params or {}
        rows = self.rows
        outer = self

        class _Result:
            def mappings(self):
                return self

            def all(self):
                return rows

            def one(self):
                return rows[0]

            def scalar_one(self):
                return len(outer.rows)

        return _Result()


def _summary_payload(**estate):
    base = {
        "datacenters": 2, "rooms": 16, "racks": 44,
        "u_total": 1848, "u_used": 456, "u_reserved": 0,
    }
    base.update(estate)
    return {
        "totals": {"assets": 664, "planned": 0, "in_service": 664,
                   "maintenance": 0, "decommissioned": 0},
        "identity": {"with_serial": 0, "with_asset_tag": 0, "unidentified": 664},
        "estate": base,
        "by_category": [],
        "discovery": {"new_candidates": 0, "unmatched": 0},
    }


def _returns(value):
    async def _call(*_args, **_kwargs):
        return value
    return _call


# ------------------------------------------------------------------ summary

@pytest.mark.asyncio
async def test_free_u_is_total_less_used_and_held(monkeypatch):
    """Held space is not free space.

    A reservation occupies rack units nobody may take, so subtracting only what
    is installed reports room that cannot be sold twice.
    """
    monkeypatch.setattr(service.repo, "summary",
                        _returns(_summary_payload(u_used=456, u_reserved=100)))

    out = await service.summary(_FakeSession())

    assert out["estate"]["u_free"] == 1848 - 456 - 100


@pytest.mark.asyncio
async def test_free_u_never_goes_negative(monkeypatch):
    """An over-subscribed estate reads zero free, not minus fifty.

    Rack heights and device heights come from different imports and can
    disagree. A negative "U free" on the landing page reads as a rendering bug
    and gets ignored; zero reads as full, which is the actionable answer.
    """
    monkeypatch.setattr(service.repo, "summary",
                        _returns(_summary_payload(u_total=100, u_used=120,
                                                  u_reserved=30)))

    out = await service.summary(_FakeSession())

    assert out["estate"]["u_free"] == 0


@pytest.mark.asyncio
async def test_summary_omits_blocks_whose_tables_do_not_exist(monkeypatch):
    """Warranty, maintenance and stock are absent, not zero.

    A tile reading "0 contracts expiring" when there is no contract table is a
    statement an operator would act on, and it would be false. Absence forces
    the UI to say "not tracked yet" instead.
    """
    monkeypatch.setattr(service.repo, "summary", _returns(_summary_payload()))

    out = await service.summary(_FakeSession())

    assert "warranty" not in out
    assert "maintenance" not in out
    assert "stock" not in out


# ------------------------------------------------------------------ filters

@pytest.mark.asyncio
async def test_lifecycle_filter_replaces_the_decommissioned_default():
    """Asking for decommissioned devices must return them.

    The list hides decommissioned rows by default. Stacking that default with
    an explicit `lifecycle=decommissioned` yields `lifecycle <> 'decommissioned'
    AND lifecycle = 'decommissioned'` - always empty, and it reads on screen as
    "there are none" rather than as a bug.
    """
    session = _FakeSession()

    await device_repo.list_devices(session, lifecycle=["decommissioned"])

    assert "d.lifecycle::text = ANY(:lifecycle)" in session.sql
    assert "d.lifecycle <> 'decommissioned'" not in session.sql
    assert session.params["lifecycle"] == ["decommissioned"]


@pytest.mark.asyncio
async def test_default_still_hides_decommissioned():
    """With no lifecycle filter, the old behaviour is untouched."""
    session = _FakeSession()

    await device_repo.list_devices(session)

    assert "d.lifecycle <> 'decommissioned'" in session.sql


@pytest.mark.asyncio
async def test_has_serial_false_is_a_filter_not_an_absence():
    """`has_serial=False` is the reconciliation queue.

    Treated as falsy-so-ignore it silently returns the whole estate, which
    looks like a healthy result rather than a missing filter.
    """
    session = _FakeSession()

    await device_repo.list_devices(session, has_serial=False)

    assert "d.serial_number IS NULL" in session.sql


@pytest.mark.asyncio
async def test_has_serial_true_selects_the_identified():
    session = _FakeSession()

    await device_repo.list_devices(session, has_serial=True)

    assert "d.serial_number IS NOT NULL" in session.sql


@pytest.mark.asyncio
async def test_has_serial_unset_adds_no_predicate():
    session = _FakeSession()

    await device_repo.list_devices(session)

    assert "d.serial_number IS NULL" not in session.sql
    assert "d.serial_number IS NOT NULL" not in session.sql


@pytest.mark.asyncio
async def test_search_covers_asset_tag():
    """An asset tag is read off a sticker and typed into the search box."""
    session = _FakeSession()

    await device_repo.list_devices(session, search="DC1-A-004")

    assert "d.asset_tag ILIKE :search" in session.sql


@pytest.mark.asyncio
async def test_asset_columns_are_selected_for_every_row():
    """The asset table renders tag, serial and lifecycle per row.

    Fetching them per device would be 200 round trips behind one screen.
    """
    session = _FakeSession()

    await device_repo.list_devices(session)

    for column in ("d.serial_number", "d.asset_tag",
                   "d.lifecycle::text AS lifecycle", "dt.category"):
        assert column in session.sql
