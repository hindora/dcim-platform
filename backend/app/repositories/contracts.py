"""Suppliers, support contracts, and the cache exactly one function may write.

`device.warranty_expires` is a denormalisation of "the latest end date among
this device's active contracts". `recompute_warranty` is the only thing allowed
to write it - every path that changes cover calls it, in the same transaction,
and nothing else touches the column. A cache with two writers is a cache that
disagrees with itself, and this one is read by a filter, a sort and a landing
page tile.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: How near expiry counts as "expiring". Defined ONCE, on the server, because
#: the tile, the filter and the asset record must not disagree about the
#: threshold - and they would, the first time somebody hard-coded 90 in a
#: component.
EXPIRING_DAYS = 90


# --------------------------------------------------------------- suppliers

async def list_suppliers(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT s.id::text, s.name, s.account_ref, s.contact_name,
               s.contact_email, s.contact_phone, s.notes,
               (SELECT count(*) FROM device d WHERE d.supplier_id = s.id)
                   AS device_count,
               (SELECT count(*) FROM support_contract c WHERE c.supplier_id = s.id)
                   AS contract_count
        FROM supplier s
        ORDER BY s.name
    """))).mappings().all()
    return [dict(r) for r in rows]


async def create_supplier(session: AsyncSession, **fields: Any) -> str:
    return (await session.execute(text("""
        INSERT INTO supplier (name, account_ref, contact_name, contact_email,
                              contact_phone, notes)
        VALUES (:name, :account_ref, :contact_name, :contact_email,
                :contact_phone, :notes)
        RETURNING id::text
    """), fields)).scalar_one()


async def delete_supplier(session: AsyncSession, supplier_id: str) -> None:
    await session.execute(text("DELETE FROM supplier WHERE id = CAST(:id AS uuid)"),
                          {"id": supplier_id})


# --------------------------------------------------------------- contracts

_CONTRACT_SELECT = """
    SELECT c.id::text, c.supplier_id::text AS supplier_id, s.name AS supplier_name,
           c.reference, c.kind, c.service_level, c.start_date, c.end_date,
           c.cost, c.currency, c.auto_renew, c.notes,
           (SELECT count(*) FROM device_support ds WHERE ds.contract_id = c.id)
               AS device_count,
           -- Derived here so the list, the detail page and the KPI tile cannot
           -- disagree about what "expiring" means.
           CASE WHEN c.end_date < CURRENT_DATE THEN 'expired'
                WHEN c.end_date <= CURRENT_DATE + CAST(:expiring AS integer) THEN 'expiring'
                ELSE 'active' END AS state,
           (c.end_date - CURRENT_DATE) AS days_remaining
    FROM support_contract c
    LEFT JOIN supplier s ON s.id = c.supplier_id
"""


async def list_contracts(session: AsyncSession, *, kind: str | None = None,
                         supplier_id: str | None = None,
                         state: str | None = None,
                         limit: int = 200) -> list[dict[str, Any]]:
    where, params = [], {"limit": limit, "expiring": EXPIRING_DAYS}
    if kind:
        where.append("c.kind = :kind")
        params["kind"] = kind
    if supplier_id:
        where.append("c.supplier_id = CAST(:supplier_id AS uuid)")
        params["supplier_id"] = supplier_id
    if state == "expired":
        where.append("c.end_date < CURRENT_DATE")
    elif state == "expiring":
        where.append("c.end_date >= CURRENT_DATE "
                     "AND c.end_date <= CURRENT_DATE + CAST(:expiring AS integer)")
    elif state == "active":
        where.append("c.end_date > CURRENT_DATE + CAST(:expiring AS integer)")

    sql = _CONTRACT_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    # Expiry ascending: the only sort anybody wants on this table.
    sql += " ORDER BY c.end_date LIMIT :limit"
    rows = (await session.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


async def get_contract(session: AsyncSession, contract_id: str) -> dict[str, Any] | None:
    row = (await session.execute(
        text(_CONTRACT_SELECT + " WHERE c.id = CAST(:id AS uuid)"),
        {"id": contract_id, "expiring": EXPIRING_DAYS})).mappings().first()
    return dict(row) if row else None


async def create_contract(session: AsyncSession, **fields: Any) -> str:
    return (await session.execute(text("""
        INSERT INTO support_contract (supplier_id, reference, kind, service_level,
                                      start_date, end_date, cost, currency,
                                      auto_renew, notes)
        VALUES (CAST(:supplier_id AS uuid), :reference, :kind, :service_level,
                :start_date, :end_date, :cost, :currency, :auto_renew, :notes)
        RETURNING id::text
    """), fields)).scalar_one()


async def update_contract(session: AsyncSession, contract_id: str,
                          changes: dict[str, Any]) -> None:
    if not changes:
        return
    sets = ", ".join(f"{k} = :{k}" for k in changes)
    await session.execute(
        text(f"UPDATE support_contract SET {sets}, updated_at = now() "
             f"WHERE id = CAST(:id AS uuid)"),
        {**changes, "id": contract_id})


async def delete_contract(session: AsyncSession, contract_id: str) -> list[str]:
    """Drop the contract, returning the devices whose cover just changed."""
    covered = await contract_devices(session, contract_id)
    await session.execute(
        text("DELETE FROM support_contract WHERE id = CAST(:id AS uuid)"),
        {"id": contract_id})
    return [d["id"] for d in covered]


async def contract_devices(session: AsyncSession, contract_id: str,
                           limit: int = 500) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT d.id::text, d.name, d.device_type, d.serial_number,
               d.warranty_expires
        FROM device_support ds
        JOIN device d ON d.id = ds.device_id
        WHERE ds.contract_id = CAST(:id AS uuid)
        ORDER BY d.name
        LIMIT :limit
    """), {"id": contract_id, "limit": limit})).mappings().all()
    return [dict(r) for r in rows]


async def device_contracts(session: AsyncSession, device_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(text(
        _CONTRACT_SELECT + """
        JOIN device_support ds ON ds.contract_id = c.id
        WHERE ds.device_id = CAST(:device_id AS uuid)
        ORDER BY c.end_date DESC
    """), {"device_id": device_id, "expiring": EXPIRING_DAYS})).mappings().all()
    return [dict(r) for r in rows]


async def cover_devices(session: AsyncSession, contract_id: str,
                        device_ids: list[str]) -> int:
    if not device_ids:
        return 0
    await session.execute(text("""
        INSERT INTO device_support (device_id, contract_id)
        SELECT CAST(d AS uuid), CAST(:cid AS uuid)
        FROM unnest(CAST(:ids AS text[])) AS d
        ON CONFLICT DO NOTHING
    """), {"cid": contract_id, "ids": device_ids})
    return len(device_ids)


async def uncover_device(session: AsyncSession, contract_id: str,
                         device_id: str) -> None:
    await session.execute(text("""
        DELETE FROM device_support
        WHERE contract_id = CAST(:cid AS uuid) AND device_id = CAST(:did AS uuid)
    """), {"cid": contract_id, "did": device_id})


# ------------------------------------------------------------- the one writer

async def recompute_warranty(session: AsyncSession,
                             device_ids: list[str] | None = None) -> int:
    """Re-derive `device.warranty_expires` from the contracts that cover it.

    MAX, not MIN. With cover to 2027 and to 2029 a device is covered until 2029;
    the earliest end date is when the FIRST contract lapses, which is a
    different question and not the one an asset list asks.

    Only contracts that have STARTED count. A contract signed for next quarter
    is not cover today, and reporting it as such is how a machine goes to site
    believing it has support it cannot yet claim.

    Passing no ids rewrites the whole estate - used after a contract's dates
    change, where the affected set is whatever that contract covers.
    """
    scope = ""
    params: dict[str, Any] = {}
    if device_ids is not None:
        if not device_ids:
            return 0
        scope = " WHERE d.id = ANY(CAST(:ids AS uuid[]))"
        params["ids"] = device_ids

    result = await session.execute(text(f"""
        UPDATE device d
           SET warranty_expires = (
                SELECT MAX(c.end_date)
                FROM device_support ds
                JOIN support_contract c ON c.id = ds.contract_id
                WHERE ds.device_id = d.id
                  AND c.start_date <= CURRENT_DATE
           ),
               updated_at = now()
        {scope}
    """), params)
    return result.rowcount or 0


async def expiry_counts(session: AsyncSession) -> dict[str, int]:
    """The landing-page tile, from the same threshold the filters use."""
    row = (await session.execute(text("""
        SELECT count(*) FILTER (WHERE warranty_expires IS NULL)        AS unknown,
               count(*) FILTER (WHERE warranty_expires < CURRENT_DATE) AS expired,
               count(*) FILTER (WHERE warranty_expires >= CURRENT_DATE
                                  AND warranty_expires <= CURRENT_DATE + CAST(:expiring AS integer))
                                                                        AS expiring,
               count(*) FILTER (WHERE warranty_expires > CURRENT_DATE + CAST(:expiring AS integer))
                                                                        AS active
        FROM device
        WHERE lifecycle NOT IN ('decommissioned', 'retired')
    """), {"expiring": EXPIRING_DAYS})).mappings().one()
    return dict(row)
