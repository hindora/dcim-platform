"""Device endpoints. Routing and validation only - logic lives in the service."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.logging import get_logger
from app.core.security import Principal, current_principal, require_role
from app.db.session import get_session
from app.repositories import contracts as contracts_repo
from app.repositories import lifecycle as lifecycle_repo
from app.repositories import maintenance as maintenance_repo
from app.schemas import (
    DeviceDetail,
    DeviceStateOut,
    DeviceSummary,
    EndpointSummary,
    HistoryOut,
    InterfaceOut,
    Page,
)
from app.services import dashboard as dashboard_service
from app.services import devices as service
from app.services import endpoint_config
from app.services import lifecycle as lifecycle_service

router = APIRouter(prefix="/devices", tags=["devices"])
log = get_logger("api.devices")


@router.get("", response_model=Page[DeviceSummary], summary="List devices")
async def list_devices(
    device_type: list[str] | None = Query(None),
    status_filter: list[str] | None = Query(None, alias="status"),
    room_id: str | None = None,
    rack_id: str | None = None,
    datacenter_id: str | None = None,
    search: str | None = Query(None, max_length=128),
    include_decommissioned: bool = False,
    # Asset-view filters (docs/21 §2). All optional and AND-combined, so a
    # caller that sends none of them gets exactly the response it got before
    # the asset module existed.
    lifecycle: list[str] | None = Query(None, description="repeatable"),
    category: list[str] | None = Query(None, description="repeatable"),
    vendor_id: str | None = None,
    asset_tag: str | None = Query(None, max_length=128),
    serial_number: str | None = Query(None, max_length=128),
    has_serial: bool | None = Query(
        None, description="false lists what still needs reconciling"),
    warranty_state: str | None = Query(
        None, pattern="^(active|expiring|expired|unknown)$"),
    warranty_before: str | None = None,
    supplier_id: str | None = None,
    owner_group: str | None = None,
    cost_centre: str | None = None,
    tag: list[str] | None = Query(
        None, description="repeatable key:value; AND-combined"),
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
    offset: int | None = Query(
        None, ge=0,
        description="jump straight to a position. Mutually exclusive with "
                    "cursor: they are two answers to 'where does this page "
                    "start' and honouring both would silently pick one"),
    with_total: bool = Query(
        False, description="also count every row the filters match; the paging "
                           "UI needs it, a plain next-page fetch does not"),
    order_by: str | None = Query(
        None,
        pattern="^(name|asset_tag|device_type|model|location|lifecycle|cover|serial)$",
        description="column to order by; default is name"),
    desc: bool = Query(False, description="reverse the order"),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> Page[DeviceSummary]:
    if cursor and offset:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "pass cursor or offset, not both")
    if cursor and (order_by or desc):
        # The cursor encodes a position in the DEFAULT order; following it
        # under another order would page through a list that no longer exists.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "a cursor cannot be combined with order_by/desc - "
            "use offset paging to sort")
    items, next_cursor, total = await service.list_devices(
        session, with_total=with_total,
        device_types=device_type, status=status_filter, room_id=room_id,
        rack_id=rack_id, datacenter_id=datacenter_id, search=search,
        include_decommissioned=include_decommissioned, lifecycle=lifecycle,
        category=category, vendor_id=vendor_id, asset_tag=asset_tag,
        serial_number=serial_number, has_serial=has_serial,
        warranty_state=warranty_state, warranty_before=warranty_before,
        supplier_id=supplier_id, owner_group=owner_group,
        cost_centre=cost_centre, tags=tag,
        limit=limit, cursor=cursor, offset=offset,
        order_by=order_by, descending=desc)
    return Page[DeviceSummary](items=items, next_cursor=next_cursor, total=total)


@router.get("/endpoint-options",
            summary="Credentials and poll profiles an endpoint may point at")
async def endpoint_options(
    protocol: str | None = Query(None, description="narrow to one protocol"),
    q: str | None = Query(None, description="search credential names"),
    current: str | None = Query(None, description="credential to always include"),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """Everything the endpoint editor needs to render its pickers.

    Declared BEFORE /{device_id} on purpose: FastAPI matches in declaration
    order, and a static path placed after a parameterised one is never reached
    - "endpoint-options" would be looked up as a device id.

    `addressing` describes the protocol-specific fields and their legal ranges
    so the form can validate before the round trip, using the same table the
    server rejects with.
    """
    return {
        "credentials": await service.credentials(
            session, protocol=protocol, q=q, current=current),
        # The count behind the capped list. A picker that silently shows the
        # first fifty of 894 is worse than one that says which it is showing.
        "credential_total": await service.credential_count(session, protocol),
        "poll_profiles": await service.poll_profiles(session),
        "default_ports": endpoint_config.DEFAULT_PORT,
        "addressing": endpoint_config.ADDRESSING_FIELDS,
    }


@router.get("/{device_id}", response_model=DeviceDetail, summary="Device detail")
async def get_device(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> DeviceDetail:
    device = await service.get_device(session, device_id)
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")
    return device


@router.get("/{device_id}/state", response_model=DeviceStateOut,
            summary="Current state and hot metrics")
async def get_state(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> DeviceStateOut:
    state = await service.get_state(session, device_id)
    if state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "no state yet for this device")
    return state


@router.get("/{device_id}/interfaces", response_model=list[InterfaceOut])
async def list_interfaces(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> list[InterfaceOut]:
    return await service.list_interfaces(session, device_id)


@router.get("/{device_id}/metrics", summary="Latest value of every metric reported")
async def latest_metrics(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    return {"device_id": device_id,
            "metrics": await dashboard_service.latest(session, device_id)}


@router.get("/{device_id}/history", response_model=HistoryOut,
            summary="Historical telemetry")
async def history(
    device_id: str,
    metric: list[str] = Query(..., description="repeatable"),
    start: datetime | None = None,
    end: datetime | None = None,
    interval: str = Query("auto", pattern="^(auto|raw|1m|5m|1h)$"),
    agg: str = Query("avg", pattern="^(avg|min|max|last)$"),
    instance: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> HistoryOut:
    return await dashboard_service.history(
        session, device_id=device_id, metrics=metric, start=start, end=end,
        interval=interval, agg=agg, instance=instance)


class LifecycleTransition(BaseModel):
    """A move from one lifecycle state to another, with why.

    `reason` is optional in the schema and asked for in the UI: a change board
    wants it, and a transition somebody could not explain is still better
    recorded than not recorded.
    """

    model_config = {"extra": "forbid"}

    to_state: str = Field(pattern="^(planned|in_stock|installed|in_service"
                                  "|maintenance|decommissioned|retired)$")
    reason: str | None = Field(None, max_length=500)
    change_ref: str | None = Field(None, max_length=100)


@router.get("/{device_id}/lifecycle", summary="Transition history, newest first")
async def lifecycle_history(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return {
        "current": await lifecycle_repo.current_state(session, device_id),
        "allowed": list(lifecycle_service.TRANSITIONS.get(
            await lifecycle_repo.current_state(session, device_id) or "", ())),
        "events": await lifecycle_service.history(session, device_id),
    }


@router.post("/{device_id}/lifecycle", summary="Record a lifecycle transition")
async def lifecycle_transition(
    device_id: str,
    body: LifecycleTransition,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    """Move the device, write the event and the audit row, in one transaction.

    A refusal is a 409 carrying the allowed set, not a bare error: an operator
    told "no" needs to be told what IS possible, and the matrix is the only
    thing that knows.
    """
    ip, agent = audit.client_of(request)
    try:
        event = await lifecycle_service.transition(
            session, device_id=device_id, to_state=body.to_state,
            actor=audit.actor_of(principal), reason=body.reason,
            change_ref=body.change_ref, ip=ip, user_agent=agent)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found") from None
    except lifecycle_service.IllegalTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {
            "error": "illegal_transition",
            "message": str(exc),
            "current": exc.current,
            "allowed": list(exc.allowed),
        }) from None
    await session.commit()
    log.info("lifecycle transition", device_id=device_id,
             to_state=body.to_state, actor=principal.username)
    return event


@router.get("/{device_id}/contracts", summary="Cover standing on this device")
async def device_contracts(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return {"items": await contracts_repo.device_contracts(session, device_id)}


@router.get("/{device_id}/maintenance", summary="Work done on this device")
async def maintenance_records(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return {"items": await maintenance_repo.list_records(session, device_id)}


class MaintenanceRecordIn(BaseModel):
    model_config = {"extra": "forbid"}

    kind: str = Field(pattern="^(preventive|corrective|firmware|replacement)$")
    summary: str = Field(min_length=1, max_length=500)
    detail: str | None = None
    window_id: str | None = None
    parts_used: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/{device_id}/maintenance", status_code=status.HTTP_201_CREATED,
             summary="Record work done on this device")
async def add_maintenance_record(
    device_id: str,
    body: MaintenanceRecordIn,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, str]:
    record_id = await maintenance_repo.add_record(
        session, device_id=device_id, performed_by=audit.actor_of(principal),
        kind=body.kind, summary=body.summary, detail=body.detail,
        window_id=body.window_id,
        parts_used=json.dumps(body.parts_used))
    await session.commit()
    return {"id": record_id}


class EndpointPatch(BaseModel):
    """An edit to one protocol endpoint.

    Every field is optional and absence means "leave it alone", which is what
    lets the UI send only what the operator touched. `None` is a real value for
    the nullable ones: a null port follows the protocol default, a null
    credential means the endpoint needs none.
    """

    model_config = {"extra": "forbid"}

    address: str | None = Field(None, description="host address; null clears it")
    port: int | None = Field(None, ge=1, le=65535,
                             description="null follows the protocol default")
    addressing: dict[str, Any] | None = Field(
        None, description="protocol-specific: modbus unit_id, bacnet instance")
    credential_id: str | None = None
    poll_profile_id: str | None = None
    enabled: bool | None = None
    admin_state: str | None = Field(None, pattern="^(enabled|disabled|maintenance)$")


@router.get("/{device_id}/endpoints", response_model=list[EndpointSummary],
            summary="Protocol endpoints and how they are configured")
async def device_endpoints(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> list[EndpointSummary]:
    return await service.endpoints(session, device_id)


@router.patch("/{device_id}/endpoints/{endpoint_id}",
              response_model=EndpointSummary,
              summary="Change how this device is reached")
async def update_endpoint(
    device_id: str,
    endpoint_id: str,
    body: EndpointPatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> EndpointSummary:
    """Edit one endpoint's connection settings.

    The change reaches the collector on its next assignment fetch - within one
    assignment interval, with nothing restarted - because the update bumps
    `updated_at`, which the assignment version is derived from.
    """
    try:
        before, after = await service.update_endpoint(
            session, device_id=device_id, endpoint_id=endpoint_id,
            changes=body.model_dump(exclude_unset=True))
    except service.EndpointNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "no such endpoint on this device") from None
    except endpoint_config.EndpointConfigError as exc:
        # 422, not 400: the request was well-formed and the VALUE was wrong,
        # and the message is written to be shown to the operator as-is.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            str(exc)) from None

    ip, agent = audit.client_of(request)
    # Connection settings decide whether a device can be seen at all, so an
    # edit that silences one has to be attributable afterwards.
    await audit.record(session, actor=audit.actor_of(principal),
                       action="endpoint.update", target_type="device_endpoint",
                       target_id=endpoint_id, ip=ip, user_agent=agent,
                       before=before, after=after)
    await session.commit()
    log.info("endpoint updated", endpoint_id=endpoint_id, device_id=device_id,
             actor=principal.username, changed=sorted(after))
    return await service.one_endpoint(session, device_id, endpoint_id)
