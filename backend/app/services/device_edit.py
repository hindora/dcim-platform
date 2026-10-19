"""Editing a device by hand.

What an operator may change on a device, in two tiers by what a wrong value
costs:

- operator: the commercial and operational facts - owner, cost centre,
  priority, notes, purchase and support dates. A typo here mislabels a
  device; nothing else keys on it.
- admin: the identity facts - name, vendor, model, serial, asset tag and
  both addresses. Discovery reconciles on serial and management address, the
  collector dials the address, and the asset tag is the sticker on the
  chassis, so a wrong value here makes a device unfindable or merges it with
  another.

Every identity field an edit changes goes into `locked_fields`, which the
importer's upsert honours: without it the next re-import puts the export's
value back and the edit looks like it never happened.
"""

from __future__ import annotations

import ipaddress
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import ROLES

#: Fields an operator may set.
OPERATOR_FIELDS = ("owner_group", "cost_centre", "priority", "notes",
                   "supplier_id", "purchase_order", "purchase_date",
                   "install_date", "eol_date", "eos_date")
#: Fields only an admin may set - the ones other systems reconcile on.
ADMIN_FIELDS = ("name", "vendor", "model", "serial_number", "asset_tag",
                "mgmt_ip", "primary_ip")
EDITABLE = OPERATOR_FIELDS + ADMIN_FIELDS
#: The identity fields the importer would otherwise overwrite.
LOCKABLE = ("name", "vendor", "model", "serial_number", "mgmt_ip", "primary_ip")
PRIORITIES = ("high", "normal", "low")
_DATES = ("purchase_date", "install_date", "eol_date", "eos_date")
_IPS = ("mgmt_ip", "primary_ip")
_TEXT = ("name", "serial_number", "asset_tag", "owner_group", "cost_centre",
         "purchase_order", "notes")

_RANK = {r: i for i, r in enumerate(ROLES)}


class DeviceEditError(Exception):
    """The edit cannot be made. The message is written for the operator."""

    def __init__(self, message: str, code: int = 422) -> None:
        super().__init__(message)
        self.code = code


class DeviceNotFoundError(Exception):
    pass


def check_role(role: str, changes: dict[str, Any]) -> None:
    """Refuse an identity edit from anyone below admin, naming the fields."""
    need = sorted(set(changes) & set(ADMIN_FIELDS))
    if need and _RANK.get(role, 0) < _RANK["admin"]:
        one = len(need) == 1
        raise DeviceEditError(
            f"{', '.join(need)} {'is an identity field' if one else 'are identity fields'}: "
            f"changing {'it' if one else 'them'} needs an admin account, because "
            f"discovery and the collector key on {'it' if one else 'them'}",
            code=403)


def normalise(changes: dict[str, Any]) -> dict[str, Any]:
    """Validate and clean the values. Empty strings mean 'clear it'."""
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise DeviceEditError(
            f"{', '.join(unknown)} cannot be edited here; editable: {', '.join(EDITABLE)}. "
            f"Placement goes through the move action and state through lifecycle.")
    out: dict[str, Any] = {}
    for k, v in changes.items():
        if isinstance(v, str):
            v = v.strip()
        if k in _TEXT:
            if k == "name" and not v:
                raise DeviceEditError("a device needs a name")
            out[k] = v or None
        elif k in ("vendor", "model"):
            out[k] = v or None
        elif k == "priority":
            if v not in PRIORITIES:
                raise DeviceEditError(f"priority must be one of {', '.join(PRIORITIES)}")
            out[k] = v
        elif k in _IPS:
            if v:
                try:
                    v = str(ipaddress.ip_address(v))
                except ValueError:
                    raise DeviceEditError(f"{v!r} is not an IP address") from None
            out[k] = v or None
        elif k in _DATES:
            if v:
                try:
                    v = date.fromisoformat(str(v))
                except ValueError:
                    raise DeviceEditError(f"{k} must be a date like 2027-03-31") from None
            out[k] = v or None
        else:
            out[k] = v or None
    return out


async def _vendor_id(session: AsyncSession, name: str) -> str:
    return (await session.execute(text("""
        INSERT INTO vendor (name) VALUES (:n)
        ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
        RETURNING id::text"""), {"n": name})).scalar_one()


async def _model_id(session: AsyncSession, vendor_id: str, name: str,
                    device_type: str) -> str:
    return (await session.execute(text("""
        INSERT INTO model (vendor_id, device_type, name)
        VALUES (CAST(:v AS uuid), :t, :n)
        ON CONFLICT (vendor_id, name) DO UPDATE SET name = EXCLUDED.name
        RETURNING id::text"""), {"v": vendor_id, "t": device_type, "n": name})).scalar_one()


_CURRENT = """
    SELECT d.id::text, d.name, d.device_type, d.vendor_id::text, d.model_id::text,
           v.name AS vendor, m.name AS model, d.serial_number, d.asset_tag,
           host(d.mgmt_ip) AS mgmt_ip, host(d.primary_ip) AS primary_ip,
           d.owner_group, d.cost_centre, d.priority, d.notes, d.supplier_id::text,
           d.purchase_order, d.purchase_date, d.install_date, d.eol_date, d.eos_date,
           d.locked_fields
      FROM device d
      LEFT JOIN vendor v ON v.id = d.vendor_id
      LEFT JOIN model m  ON m.id = d.model_id
     WHERE d.id = CAST(:id AS uuid)"""


async def current(session: AsyncSession, device_id: str) -> dict[str, Any] | None:
    row = (await session.execute(text(_CURRENT), {"id": device_id})).mappings().first()
    return dict(row) if row else None


async def apply(session: AsyncSession, device_id: str,
                changes: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply validated changes. Returns (before, after) for only what changed,
    JSON-safe, for the audit row. Raises DeviceEditError on a conflict."""
    row = await current(session, device_id)
    if row is None:
        raise DeviceNotFoundError(device_id)

    sets: dict[str, Any] = {}
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    def note(field: str, old: Any, new: Any) -> None:
        before[field] = None if old is None else str(old)
        after[field] = None if new is None else str(new)

    # Vendor and model resolve to rows, creating them when the name is new.
    vendor_changed = "vendor" in changes and changes["vendor"] != row["vendor"]
    model_changed = "model" in changes and changes["model"] != row["model"]
    if vendor_changed or model_changed:
        vname = changes.get("vendor", row["vendor"])
        mname = changes.get("model", row["model"])
        if vendor_changed:
            note("vendor", row["vendor"], vname)
        if model_changed:
            note("model", row["model"], mname)
        if mname and not vname:
            raise DeviceEditError("a model belongs to a vendor: set the vendor too")
        if vendor_changed and not model_changed and row["model"]:
            # The old model belonged to the old vendor: keeping it would pair
            # a vendor with a model it never made.
            raise DeviceEditError(
                "changing the vendor leaves the model with the wrong maker: "
                "set the model in the same edit")
        sets["vendor_id"] = await _vendor_id(session, vname) if vname else None
        if mname:
            sets["model_id"] = await _model_id(session, sets["vendor_id"], mname,
                                               row["device_type"])
        else:
            sets["model_id"] = None

    for k, new in changes.items():
        if k in ("vendor", "model"):
            continue
        old = row.get(k)
        if k in _DATES and old is not None:
            old = old.isoformat() if hasattr(old, "isoformat") else old
        if (None if old is None else str(old)) == (None if new is None else str(new)):
            continue
        note(k, old, new)
        sets[k] = new

    if not before:
        return {}, {}

    locked = set(row["locked_fields"] or [])
    locked |= {f for f in before if f in LOCKABLE}
    sets["locked_fields"] = sorted(locked)

    assignments = []
    params: dict[str, Any] = {"id": device_id}
    for k, v in sets.items():
        p = f"p_{k}"
        params[p] = v
        if k in _IPS:
            assignments.append(f"{k} = CAST(:{p} AS inet)")
        elif k in ("vendor_id", "model_id", "supplier_id"):
            assignments.append(f"{k} = CAST(:{p} AS uuid)")
        elif k == "locked_fields":
            assignments.append(f"{k} = CAST(:{p} AS text[])")
        else:
            assignments.append(f"{k} = :{p}")
    try:
        async with session.begin_nested():
            await session.execute(text(
                f"UPDATE device SET {', '.join(assignments)}, updated_at = now() "
                f"WHERE id = CAST(:id AS uuid)"), params)
    except IntegrityError as exc:
        raise DeviceEditError(_conflict(str(exc.orig)), code=409) from None
    return before, after


def _conflict(detail: str) -> str:
    if "mgmt_ip" in detail:
        return "another live device already has that management address"
    if "serial" in detail:
        return "another device already has that serial number"
    if "asset_tag" in detail:
        return "another device already has that asset tag"
    if "supplier" in detail:
        return "there is no such supplier"
    return "the change conflicts with another device's record"


async def unlock(session: AsyncSession, device_id: str, fields: list[str]) -> list[str]:
    """Hand fields back to the importer. Returns the fields now locked."""
    row = await current(session, device_id)
    if row is None:
        raise DeviceNotFoundError(device_id)
    bad = sorted(set(fields) - set(LOCKABLE))
    if bad:
        raise DeviceEditError(f"{', '.join(bad)} are not lockable fields")
    left = sorted(set(row["locked_fields"] or []) - set(fields))
    await session.execute(text(
        "UPDATE device SET locked_fields = CAST(:l AS text[]), updated_at = now() "
        "WHERE id = CAST(:id AS uuid)"), {"l": left, "id": device_id})
    return left
