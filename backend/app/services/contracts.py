"""Contracts, and the invariant that `warranty_expires` never goes stale.

Every function here that can change what covers a device recomputes the cache
before returning, in the same transaction. That is the whole reason this module
exists rather than the router calling the repository directly: the recompute is
easy to forget at a call site and impossible to forget behind one door.

The paths that change cover, all of them:
  * a device is added to a contract
  * a device is removed from a contract
  * a contract's start or end date moves
  * a contract is deleted
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import contracts as repo

#: Re-exported so the API and the tests read the threshold from one place.
EXPIRING_DAYS = repo.EXPIRING_DAYS


class ContractError(ValueError):
    """Bad request, with a message meant for the caller."""


async def create(session: AsyncSession, fields: dict[str, Any],
                 device_ids: list[str] | None = None) -> dict[str, Any]:
    if fields["end_date"] < fields["start_date"]:
        raise ContractError("a contract cannot end before it starts")
    contract_id = await repo.create_contract(session, **fields)
    if device_ids:
        await repo.cover_devices(session, contract_id, device_ids)
        await repo.recompute_warranty(session, device_ids)
    return await repo.get_contract(session, contract_id)


async def update(session: AsyncSession, contract_id: str,
                 changes: dict[str, Any]) -> dict[str, Any]:
    before = await repo.get_contract(session, contract_id)
    if before is None:
        raise LookupError("no such contract")

    start = changes.get("start_date", before["start_date"])
    end = changes.get("end_date", before["end_date"])
    if end < start:
        raise ContractError("a contract cannot end before it starts")

    await repo.update_contract(session, contract_id, changes)

    # Only a date move changes cover. Renaming the reference or editing a note
    # does not, and rewriting two hundred device rows for it would be waste -
    # but getting this wrong in the other direction leaves the cache stale, so
    # the test asserts the dates are in this set.
    if {"start_date", "end_date"} & set(changes):
        covered = [d["id"] for d in await repo.contract_devices(session, contract_id)]
        await repo.recompute_warranty(session, covered)
    return await repo.get_contract(session, contract_id)


async def delete(session: AsyncSession, contract_id: str) -> int:
    """Devices lose this cover, so their cached expiry is re-derived from what
    is left - which may be nothing, and NULL is the honest answer then."""
    affected = await repo.delete_contract(session, contract_id)
    await repo.recompute_warranty(session, affected)
    return len(affected)


async def cover(session: AsyncSession, contract_id: str,
                device_ids: list[str]) -> int:
    if await repo.get_contract(session, contract_id) is None:
        raise LookupError("no such contract")
    added = await repo.cover_devices(session, contract_id, device_ids)
    await repo.recompute_warranty(session, device_ids)
    return added


async def uncover(session: AsyncSession, contract_id: str, device_id: str) -> None:
    await repo.uncover_device(session, contract_id, device_id)
    await repo.recompute_warranty(session, [device_id])
