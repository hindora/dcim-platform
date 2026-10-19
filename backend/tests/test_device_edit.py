"""Editing a device by hand: what is allowed, by whom, and that it sticks."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import device_edit as de

SRC = Path(__file__).resolve().parents[1] / "app"


def test_an_operator_may_not_change_identity_and_is_told_which_field():
    with pytest.raises(de.DeviceEditError) as e:
        de.check_role("operator", {"owner_group": "x", "serial_number": "ABC"})
    assert e.value.code == 403
    assert "serial_number" in str(e.value) and "owner_group" not in str(e.value)


def test_an_admin_may_change_identity_and_an_operator_the_rest():
    de.check_role("admin", {"name": "n", "mgmt_ip": "10.0.0.1", "owner_group": "o"})
    de.check_role("operator", {"owner_group": "o", "priority": "high", "notes": "n"})


def test_every_identity_field_the_importer_overwrites_is_lockable():
    # Anything an admin can set that the upsert writes must be lockable, or the
    # edit is undone on the next re-import.
    overwritten = {"name", "vendor", "model", "serial_number", "mgmt_ip", "primary_ip"}
    assert overwritten <= set(de.LOCKABLE)
    # asset_tag is not written by the importer at all, so it needs no lock.
    assert "asset_tag" not in de.LOCKABLE


def test_the_importer_upsert_keeps_a_locked_value():
    sql = (SRC / "importer" / "simulator.py").read_text(encoding="utf-8")
    start = sql.index("async def _upsert_device(")
    body = sql[start:sql.index("RETURNING id::text", start)]
    for field in ("name", "serial_number", "mgmt_ip", "primary_ip", "vendor", "model"):
        assert f"'{field}' = ANY(device.locked_fields)" in body, field


def test_unknown_fields_are_refused_with_the_editable_list():
    with pytest.raises(de.DeviceEditError) as e:
        de.normalise({"rack_id": "x"})
    assert "rack_id" in str(e.value) and "move" in str(e.value)


def test_values_are_checked_and_cleaned():
    out = de.normalise({"name": "  SRV01 ", "mgmt_ip": "10.0.0.5", "priority": "high",
                        "owner_group": "", "purchase_date": "2027-03-31"})
    assert out["name"] == "SRV01"
    assert out["mgmt_ip"] == "10.0.0.5"
    assert out["owner_group"] is None            # empty clears
    assert out["purchase_date"].isoformat() == "2027-03-31"
    for bad in ({"name": "  "}, {"mgmt_ip": "10.0.0.999"}, {"priority": "urgent"},
                {"install_date": "next week"}):
        with pytest.raises(de.DeviceEditError):
            de.normalise(bad)


def test_a_conflict_is_reported_as_the_operator_would_say_it():
    assert "management address" in de._conflict('duplicate key "ix_device_mgmt_ip_live"')
    assert "serial" in de._conflict('duplicate key "ix_device_serial_unique"')
    assert "asset tag" in de._conflict('duplicate key "ix_device_asset_tag_unique"')


def test_priority_and_locks_are_columns_with_a_check():
    mig = (SRC.parent / "alembic" / "versions"
           / "0107_a_device_can_be_edited_by_hand.py").read_text(encoding="utf-8")
    assert "'high', 'normal', 'low'" in mig and "locked_fields" in mig
    assert 'drop_constraint("priority_known", "device", type_="check")' in mig
