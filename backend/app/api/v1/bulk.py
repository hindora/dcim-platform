"""Bulk operations over assets, and CSV in and out.

Three properties hold for every endpoint here, and they are the contract rather
than implementation detail (docs/21 §10):

  * per-row transactions, so a failure on row 3 keeps rows 1 and 2;
  * a row-level report, never a bare count;
  * one audit row and one lifecycle event PER DEVICE, not per batch. A bulk
    decommission of forty devices is forty audit rows or it is not an audit
    trail.
"""

from __future__ import annotations

import csv
import hashlib
import io
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.logging import get_logger
from app.core.security import Principal, current_principal, require_role
from app.db.session import get_session
from app.repositories import devices as device_repo
from app.repositories import lifecycle as lifecycle_repo
from app.repositories import reservations as res_repo
from app.repositories import tags as tag_repo
from app.services import bulk as engine
from app.services import lifecycle as lifecycle_service

router = APIRouter(prefix="/assets/bulk", tags=["bulk"])
log = get_logger("api.bulk")

#: Fields a bulk edit may set. Deliberately short: anything that changes where a
#: device IS goes through /move, which has to reason about rack units, and
#: anything that changes what it IS goes through /lifecycle, which has a matrix.
EDITABLE = ("owner_group", "cost_centre", "supplier_id", "purchase_order",
            "purchase_date", "install_date", "eol_date", "eos_date", "notes")


class BulkLifecycle(BaseModel):
    model_config = {"extra": "forbid"}
    device_ids: list[str] = Field(min_length=1, max_length=1000)
    to_state: str = Field(pattern="^(planned|in_stock|installed|in_service"
                                  "|maintenance|decommissioned|retired)$")
    reason: str | None = Field(None, max_length=500)
    change_ref: str | None = Field(None, max_length=100)


class BulkTags(BaseModel):
    model_config = {"extra": "forbid"}
    device_ids: list[str] = Field(min_length=1, max_length=1000)
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class BulkFields(BaseModel):
    model_config = {"extra": "forbid"}
    device_ids: list[str] = Field(min_length=1, max_length=1000)
    set: dict[str, Any]


class Move(BaseModel):
    model_config = {"extra": "forbid"}
    device_id: str
    rack_id: str | None = None
    u_start: int | None = Field(None, ge=1)


class BulkMove(BaseModel):
    model_config = {"extra": "forbid"}
    moves: list[Move] = Field(min_length=1, max_length=1000)
    #: All-or-nothing. Moving half a rack is sometimes worse than moving none of
    #: it, which is the one case where a batch should fail whole.
    atomic: bool = False


async def _names(session: AsyncSession, device_ids: list[str]) -> dict[str, str]:
    """Names up front, so a failure can say WHICH device without a query per row
    - and so it still has the name after a savepoint rolled back."""
    if not device_ids:
        return {}
    rows = (await session.execute(text(
        "SELECT id::text, name FROM device WHERE id = ANY(CAST(:ids AS uuid[]))"
    ), {"ids": device_ids})).mappings().all()
    return {r["id"]: r["name"] for r in rows}


def _report(report: engine.BulkReport) -> dict[str, Any]:
    return report.as_dict()


@router.post("/lifecycle", summary="Move many assets through their lifecycle")
async def bulk_lifecycle(
    body: BulkLifecycle,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    actor = audit.actor_of(principal)
    ip, agent = audit.client_of(request)
    names = await _names(session, body.device_ids)

    async def apply(item: dict[str, Any]) -> None:
        # The single-device service, per row. Sharing it is what guarantees the
        # bulk path cannot drift from the matrix, and that each device gets its
        # own lifecycle event AND its own audit row.
        await lifecycle_service.transition(
            session, device_id=item["device_id"], to_state=body.to_state,
            actor=actor, reason=body.reason, change_ref=body.change_ref,
            ip=ip, user_agent=agent)

    def describe(item: dict[str, Any]) -> tuple[str, str | None]:
        return item["device_id"], names.get(item["device_id"])

    async def enrich(item: dict[str, Any], exc: Exception) -> str | None:
        if isinstance(exc, lifecycle_repo.IllegalTransitionError):
            return str(exc)
        return None

    report = await engine.run(
        session, [{"device_id": d} for d in body.device_ids], apply,
        describe=describe, enrich=enrich)
    await session.commit()
    log.info("bulk lifecycle", to_state=body.to_state, actor=principal.username,
             succeeded=report.succeeded, failed=len(report.failed))
    return _report(report)


@router.post("/tags", summary="Attach and detach tags across many assets")
async def bulk_tags(
    body: BulkTags,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    if not body.add and not body.remove:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "nothing to add or remove")
    actor = audit.actor_of(principal)
    names = await _names(session, body.device_ids)

    async def apply(item: dict[str, Any]) -> None:
        device_id = item["device_id"]
        if body.add:
            await tag_repo.assign(session, object_type="device",
                                  object_id=device_id, tag_ids=body.add,
                                  actor=actor)
        for tag_id in body.remove:
            await tag_repo.unassign(session, object_type="device",
                                    object_id=device_id, tag_id=tag_id)

    report = await engine.run(
        session, [{"device_id": d} for d in body.device_ids], apply,
        describe=lambda i: (i["device_id"], names.get(i["device_id"])))
    await session.commit()
    return _report(report)


@router.post("/fields", summary="Set ownership and purchase fields in bulk")
async def bulk_fields(
    body: BulkFields,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    unknown = sorted(set(body.set) - set(EDITABLE))
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {
            "error": "field_not_bulk_editable",
            "message": f"{', '.join(unknown)} cannot be set in bulk; "
                       f"placement goes through /move and state through "
                       f"/lifecycle",
            "editable": list(EDITABLE),
        })
    if not body.set:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "nothing to set")

    actor = audit.actor_of(principal)
    ip, agent = audit.client_of(request)
    names = await _names(session, body.device_ids)
    assignments = ", ".join(f"{k} = :{k}" for k in body.set)

    async def apply(item: dict[str, Any]) -> None:
        device_id = item["device_id"]
        await session.execute(
            text(f"UPDATE device SET {assignments}, updated_at = now() "
                 f"WHERE id = CAST(:id AS uuid)"),
            {**body.set, "id": device_id})
        await audit.record(session, actor=actor, action="device.bulk_edit",
                           target_type="device", target_id=device_id,
                           ip=ip, user_agent=agent, before=None, after=body.set)

    report = await engine.run(
        session, [{"device_id": d} for d in body.device_ids], apply,
        describe=lambda i: (i["device_id"], names.get(i["device_id"])))
    await session.commit()
    return _report(report)


@router.post("/move", summary="Re-rack many assets")
async def bulk_move(
    body: BulkMove,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    actor = audit.actor_of(principal)
    ip, agent = audit.client_of(request)
    ids = [m.device_id for m in body.moves]
    names = await _names(session, ids)

    async def apply(item: dict[str, Any]) -> None:
        await session.execute(text("""
            UPDATE device
               SET rack_id = CAST(:rack AS uuid), u_start = :u_start,
                   updated_at = now()
             WHERE id = CAST(:id AS uuid)
        """), {"id": item["device_id"], "rack": item["rack_id"],
               "u_start": item["u_start"]})
        await audit.record(session, actor=actor, action="device.bulk_move",
                           target_type="device", target_id=item["device_id"],
                           ip=ip, user_agent=agent, before=None,
                           after={"rack_id": item["rack_id"],
                                  "u_start": item["u_start"]})

    async def enrich(item: dict[str, Any], exc: Exception) -> str | None:
        # "U20-U23 is occupied by SRV-DC1-HA-R2-09" beats "those rack units are
        # already occupied": the operator needs to know WHAT is in the way.
        if "device_u_no_overlap" not in str(exc) or not item.get("rack_id"):
            return None
        height = (await session.execute(text(
            "SELECT u_height FROM device WHERE id = CAST(:id AS uuid)"
        ), {"id": item["device_id"]})).scalar_one_or_none() or 1
        who = await res_repo.occupant_of(
            session, item["rack_id"], item["u_start"], height)
        span = (f"U{item['u_start']}"
                + (f"-U{item['u_start'] + height - 1}" if height > 1 else ""))
        return f"{span} is occupied" + (f" by {who}" if who else "")

    report = await engine.run(
        session, [m.model_dump() for m in body.moves], apply,
        atomic=body.atomic,
        describe=lambda i: (i["device_id"], names.get(i["device_id"])),
        enrich=enrich)
    await session.commit()
    log.info("bulk move", actor=principal.username, atomic=body.atomic,
             succeeded=report.succeeded, failed=len(report.failed))
    return _report(report)


# ----------------------------------------------------------------- CSV

CSV_COLUMNS = ("external_id", "asset_tag", "serial_number", "name",
               "device_type", "site", "room", "rack", "u_start", "lifecycle",
               "vendor", "model", "owner_group", "cost_centre",
               "purchase_order", "purchase_date", "warranty_expires")

#: How an imported row is matched to a device, in order. First hit wins, and the
#: dry run says which key matched - so an operator can see that a row landed on
#: a device by NAME when they expected it to match on serial.
MATCH_KEYS = ("external_id", "serial_number", "asset_tag", "name")


@router.get("/export", summary="The current asset list as CSV")
async def export_csv(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
    lifecycle: list[str] | None = None,
    device_type: list[str] | None = None,
) -> StreamingResponse:
    rows, _cursor = await device_repo.list_devices(
        session, lifecycle=lifecycle, device_types=device_type, limit=500)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({
            "external_id": row.get("external_id") or "",
            "asset_tag": row.get("asset_tag") or "",
            "serial_number": row.get("serial_number") or "",
            "name": row.get("name") or "",
            "device_type": row.get("device_type") or "",
            "site": row.get("datacenter_code") or "",
            "room": row.get("room_name") or "",
            "rack": row.get("rack_name") or "",
            "u_start": row.get("u_start") or "",
            "lifecycle": row.get("lifecycle") or "",
            "vendor": row.get("vendor") or "",
            "model": row.get("model") or "",
            "owner_group": row.get("owner_group") or "",
            "cost_centre": row.get("cost_centre") or "",
            "warranty_expires": row.get("warranty_expires") or "",
        })
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="assets.csv"'})


async def _match(session: AsyncSession, row: dict[str, str]) -> tuple[str | None, str | None]:
    """Find the device this row refers to, and say by which key."""
    for key in MATCH_KEYS:
        value = (row.get(key) or "").strip()
        if not value:
            continue
        found = (await session.execute(
            text(f"SELECT id::text FROM device WHERE {key} = :v LIMIT 2"),
            {"v": value})).scalars().all()
        if len(found) == 1:
            return found[0], key
        if len(found) > 1:
            return None, f"ambiguous:{key}"
    return None, None


@router.post("/import", summary="Validate a CSV, or apply one already validated")
async def import_csv(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form("validate"),
    digest: str | None = Form(None),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    """Always two-phase.

    An import that discovers two bad rows in four hundred at write time has
    already written three hundred and ninety-eight, and the operator has no way
    to know which. So `validate` writes nothing and returns the same row-level
    report the apply would produce, plus a digest of the bytes it read.

    `apply` requires that digest back. Same file, same digest; a file edited
    between the two phases no longer matches and is refused - which is stateless
    and tamper-evident, where a server-side job would simply expire under
    somebody reviewing a long report.
    """
    if mode not in ("validate", "apply"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "mode must be validate or apply")

    raw = await file.read()
    computed = hashlib.sha256(raw).hexdigest()[:32]
    if mode == "apply" and digest != computed:
        raise HTTPException(status.HTTP_409_CONFLICT, {
            "error": "digest_mismatch",
            "message": "this file is not the one that was validated; "
                       "validate it again before applying",
        })

    try:
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    except UnicodeDecodeError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "the file is not UTF-8 text") from None
    if not rows:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "the file has no rows")

    actor = audit.actor_of(principal)
    ip, agent = audit.client_of(request)

    matched: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=2):   # 1 is the header
        device_id, key = await _match(session, row)
        if device_id is None:
            problems.append({
                "row": index,
                "name": (row.get("name") or "").strip() or None,
                "error": "ambiguous_match" if key and key.startswith("ambiguous")
                         else "no_match",
                "message": (f"more than one device has that {key.split(':')[1]}"
                            if key and key.startswith("ambiguous")
                            else "no device matches on external id, serial, "
                                 "asset tag or name"),
            })
            continue
        fields = {k: (row.get(k) or "").strip() or None
                  for k in EDITABLE if k in row}
        fields = {k: v for k, v in fields.items() if v is not None}
        # asset_tag is importable and serial is not: a tag is a sticker
        # somebody applied, a serial is what the hardware reports.
        if (row.get("asset_tag") or "").strip():
            fields["asset_tag"] = row["asset_tag"].strip()
        matched.append({"device_id": device_id, "row": index,
                        "matched_by": key, "fields": fields,
                        "name": (row.get("name") or "").strip() or None})

    if mode == "validate":
        return {
            "mode": "validate",
            "digest": computed,
            "rows": len(rows),
            "would_update": len(matched),
            "unmatched": problems,
            # Which key matched each row, so somebody can see a row landing on a
            # device by NAME when they expected serial.
            "matched_by": {k: sum(1 for m in matched if m["matched_by"] == k)
                           for k in MATCH_KEYS},
            "sample": matched[:10],
        }

    async def apply(item: dict[str, Any]) -> None:
        if not item["fields"]:
            return
        sets = ", ".join(f"{k} = :{k}" for k in item["fields"])
        await session.execute(
            text(f"UPDATE device SET {sets}, updated_at = now() "
                 f"WHERE id = CAST(:id AS uuid)"),
            {**item["fields"], "id": item["device_id"]})
        await audit.record(session, actor=actor, action="device.csv_import",
                           target_type="device", target_id=item["device_id"],
                           ip=ip, user_agent=agent, before=None,
                           after=item["fields"])

    report = await engine.run(
        session, matched, apply,
        describe=lambda i: (i["device_id"], i.get("name")))
    await session.commit()
    log.info("csv import applied", actor=principal.username,
             succeeded=report.succeeded, failed=len(report.failed),
             unmatched=len(problems))
    out = _report(report)
    out["unmatched"] = problems
    out["mode"] = "apply"
    return out
