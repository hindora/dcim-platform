"""Discovery endpoints. Routing and validation only."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.logging import get_logger
from app.core.security import Principal, current_principal, require_role
from app.db.session import get_session
from app.repositories import discovery as repo
from app.repositories import discovery_ranges as ranges_repo
from app.repositories import meter_channels as meter_repo
from app.services import discovery as service
from app.services import discovery_ranges, meter_commissioning

log = get_logger("api.discovery")
router = APIRouter(prefix="/discovery", tags=["discovery"])


class RunRequest(BaseModel):
    method: str = "sweep"
    #: Saved ranges. Each is swept by the collector assigned to it.
    range_ids: list[str] = Field(default_factory=list, max_length=128)
    #: One-off subnets, not saved. Nothing records which collector reaches them,
    #: so they go to `collector_id`, or to any collector.
    subnets: list[str] = Field(default_factory=list, max_length=64,
                               examples=[["10.51.0.0/24"]])
    collector_id: str | None = None


class RangeRequest(BaseModel):
    cidr: str
    name: str | None = None
    datacenter_id: str | None = None
    purpose: str | None = None
    collector_id: str | None = None
    exclusions: list[str] = Field(default_factory=list, max_length=256)
    enabled: bool = True
    notes: str | None = Field(default=None, max_length=2000)


class RangeUpdate(BaseModel):
    cidr: str | None = None
    name: str | None = None
    datacenter_id: str | None = None
    purpose: str | None = None
    collector_id: str | None = None
    exclusions: list[str] | None = Field(default=None, max_length=256)
    enabled: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)


class EndpointCredentialChoice(BaseModel):
    """How the new endpoint authenticates.

    `address` - the community is the device's own address (the simulator's
    convention, offered only when the sweep proved it). `existing` - a credential
    already in the store. `new` - typed here, encrypted on arrival, never echoed.
    """
    mode: Literal["existing", "new", "address"]
    id: str | None = None
    community: str | None = None
    username: str | None = None
    password: str | None = None


class EndpointRequest(BaseModel):
    #: The probe to poll - its protocol, address and port are what the sweep saw.
    candidate_id: str
    credential: EndpointCredentialChoice
    port: int | None = None
    poll_profile_id: str | None = None


class PromoteRequest(BaseModel):
    name: str
    device_type: str | None = None
    #: The endpoints to create with the record. Absent: none, as before.
    endpoints: list[EndpointRequest] | None = Field(default=None, max_length=8)
    # Fulfil an existing reservation instead of creating a record.
    #
    # The operator chooses it because there is no key to match on: a placeholder
    # has no address and no serial, which is the whole reason it is a placeholder.
    # Without this, promotion left TWO records for one machine - the placeholder
    # still holding its rack unit, and a discovered device with no placement.
    attach_to_device_id: str | None = None
    # Accept the sweep's guesses. Resolved against the catalog and never created
    # from, so an unrecognised name resolves to nothing rather than adding "DELL"
    # beside "Dell Inc.".
    vendor: str | None = None
    model: str | None = None


class BulkIgnoreRequest(BaseModel):
    candidate_ids: list[str] = Field(min_length=1, max_length=500)


class BulkPromoteRequest(BaseModel):
    """Promote several responders, each under the name it reports.

    No name field, deliberately. A bulk promote cannot ask for 40 names, and
    inventing them from a template - prefix plus last octet - would put addresses
    into the estate's naming scheme for ever. So this only takes candidates that
    ALREADY say what they are called, and the caller is told which ones it skipped.
    """

    candidate_ids: list[str] = Field(min_length=1, max_length=500)
    device_type: str | None = None
    #: Create the endpoints the sweep's evidence proves - an SNMP community that
    #: is the device's own address - and report the rest as needing a credential.
    auto_endpoints: bool = True


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED,
             summary="Queue a discovery sweep")
async def create_run(req: RunRequest,
                     request: Request,
                     session: AsyncSession = Depends(get_session),
                     principal: Principal = Depends(require_role("operator")),
                     ) -> dict[str, Any]:
    """Queues the run; a collector claims and executes it.

    Accepted rather than created: the sweep happens on the management network,
    which is where the collector is and the API is not.
    """
    try:
        runs = await discovery_ranges.queue(
            session, method=req.method, range_ids=req.range_ids,
            subnets=req.subnets, collector_id=req.collector_id)
        ip, agent = audit.client_of(request)
        # A discovery sweep is active traffic on the management network, sent
        # to addresses nobody has claimed yet. Who asked for it, over which
        # subnets and from which collector, is worth keeping - per run.
        for run in runs:
            await audit.record(session, actor=audit.actor_of(principal),
                               action="discovery.run", target_type="discovery_run",
                               target_id=str(run.get("id")), ip=ip, user_agent=agent,
                               after={"method": req.method, "scope": run.get("scope"),
                                      "collector_id": run.get("collector_id")})
        await session.commit()
        # The first run's fields at the top level, for callers that queued one
        # subnet and read one run back; `runs` is the whole answer.
        return {**runs[0], "runs": runs}
    except service.DiscoveryError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.get("/subnets", summary="Management subnets worth sweeping")
async def suggest_subnets(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """/24s inventory's addresses sit in that no saved range covers yet.

    Suggestions, not the list. They can only show space something is already
    recorded in - never the unrecorded subnet an audit is for - and a real
    management network is rarely a tidy /24. The list is the saved ranges.
    """
    return {"subnets": await ranges_repo.suggestions(session)}


# ── ranges ─────────────────────────────────────────────────────────────────


@router.get("/ranges", summary="Address ranges saved for discovery")
async def list_ranges(session: AsyncSession = Depends(get_session),
                      _: Principal = Depends(current_principal)) -> dict[str, Any]:
    return {"items": await ranges_repo.list_ranges(session)}


@router.get("/range-options", summary="What a range can be assigned to")
async def range_options(session: AsyncSession = Depends(get_session),
                        _: Principal = Depends(current_principal)) -> dict[str, Any]:
    return await discovery_ranges.options(session)


@router.post("/ranges", status_code=status.HTTP_201_CREATED,
             summary="Save an address range for discovery")
async def create_range(body: RangeRequest, request: Request,
                       session: AsyncSession = Depends(get_session),
                       principal: Principal = Depends(require_role("operator")),
                       ) -> dict[str, Any]:
    actor = audit.actor_of(principal)
    try:
        row = await discovery_ranges.create_range(session, body.model_dump(), actor)
    except service.DiscoveryError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=actor, action="discovery.range.create",
                       target_type="discovery_range", target_id=row["id"],
                       ip=ip, user_agent=agent, after=body.model_dump())
    await session.commit()
    return row


@router.patch("/ranges/{range_id}", summary="Change a discovery range")
async def update_range(range_id: str, body: RangeUpdate, request: Request,
                       session: AsyncSession = Depends(get_session),
                       principal: Principal = Depends(require_role("operator")),
                       ) -> dict[str, Any]:
    # Only what the caller sent: an absent field is "leave alone", and a
    # datacenter or collector sent as null is "clear it".
    fields = body.model_dump(exclude_unset=True)
    try:
        row = await discovery_ranges.update_range(session, range_id, fields)
    except service.DiscoveryError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="discovery.range.update",
                       target_type="discovery_range", target_id=range_id,
                       ip=ip, user_agent=agent, after=fields)
    await session.commit()
    return row


@router.delete("/ranges/{range_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Stop auditing a range")
async def delete_range(range_id: str, request: Request,
                       session: AsyncSession = Depends(get_session),
                       principal: Principal = Depends(require_role("operator")),
                       ) -> None:
    try:
        await discovery_ranges.delete_range(session, range_id)
    except service.DiscoveryError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="discovery.range.delete",
                       target_type="discovery_range", target_id=range_id,
                       ip=ip, user_agent=agent)
    await session.commit()


@router.get("/attachable", summary="Reservations a responder could be fulfilling")
async def attachable(
    device_type: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """`planned` and `in_stock` records - hardware somebody is waiting for.

    These hold a rack unit and a power budget that arriving hardware should inherit
    rather than duplicate, and they carry the placement a sweep cannot know.
    """
    return {"items": await repo.attachable_devices(session, device_type=device_type)}


@router.get("/runs", summary="List discovery runs")
async def list_runs(limit: int = Query(25, ge=1, le=100),
                    session: AsyncSession = Depends(get_session),
                    _: Principal = Depends(current_principal)) -> dict[str, Any]:
    return {"items": await repo.list_runs(session, limit)}


@router.get("/candidates", summary="What answered, and whether we knew about it")
async def list_candidates(
    run_id: str | None = None,
    candidate_status: str | None = Query(None, alias="status"),
    unmatched_only: bool = Query(
        False, description="Only responders inventory has never heard of"),
    limit: int = Query(200, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    items = await repo.list_candidates(
        session, run_id=run_id, status=candidate_status,
        unmatched_only=unmatched_only, limit=limit)
    return {"items": items,
            "unmanaged": sum(1 for i in items if not i["matched_device_id"])}


@router.post("/candidates/{candidate_id}/promote",
             summary="Create an inventory device from a candidate")
async def promote(candidate_id: str, req: PromoteRequest, request: Request,
                  session: AsyncSession = Depends(get_session),
                  principal: Principal = Depends(require_role("operator")),
                  ) -> dict[str, Any]:
    try:
        result = await service.promote(session, candidate_id, req.model_dump(),
                                       actor=audit.actor_of(principal))
        ip, agent = audit.client_of(request)
        # Promotion creates an inventory device from something found on the
        # wire. scrub() runs over the payload on the way in, because a promote
        # body can carry the credential the device answered with.
        await audit.record(session, actor=audit.actor_of(principal),
                           action="discovery.promote", target_type="candidate",
                           target_id=candidate_id, ip=ip, user_agent=agent,
                           after=req.model_dump())
        await session.commit()
        return result
    except service.DiscoveryError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.get("/candidates/{candidate_id}/monitoring",
            summary="The endpoints promoting this responder would create")
async def monitoring_plan(candidate_id: str,
                          device_type: str | None = Query(None),
                          session: AsyncSession = Depends(get_session),
                          _: Principal = Depends(current_principal)
                          ) -> dict[str, Any]:
    """Server-decided, so the dialog cannot disagree with the importer about which
    agent a probe is or which profile polls it. Carries a credential suggestion
    only where the sweep's own evidence supports one."""
    try:
        return await service.monitoring_plan(session, candidate_id, device_type)
    except service.DiscoveryError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None


@router.post("/candidates/{candidate_id}/ignore",
             summary="Dismiss a candidate")
async def ignore(candidate_id: str, request: Request,
                 session: AsyncSession = Depends(get_session),
                 principal: Principal = Depends(require_role("operator")),
                 ) -> dict[str, Any]:
    try:
        result = await service.ignore(session, candidate_id)
        ip, agent = audit.client_of(request)
        # Dismissing a responder is a security-relevant decision: it is how a
        # device that answers on the management network stops being asked about.
        await audit.record(session, actor=audit.actor_of(principal),
                           action="discovery.ignore", target_type="candidate",
                           target_id=candidate_id, ip=ip, user_agent=agent)
        await session.commit()
        return result
    except service.DiscoveryError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.post("/candidates/{candidate_id}/unignore",
             summary="Put a dismissed candidate back in the queue")
async def unignore(candidate_id: str, request: Request,
                   session: AsyncSession = Depends(get_session),
                   principal: Principal = Depends(require_role("operator")),
                   ) -> dict[str, Any]:
    """Audited for the same reason ignoring is.

    Dismissing a responder is how a device that answers on the management network
    stops being asked about; restoring one is somebody deciding that call was
    wrong. Both are security-relevant and both belong on the trail.
    """
    try:
        result = await service.unignore(session, candidate_id)
        ip, agent = audit.client_of(request)
        await audit.record(session, actor=audit.actor_of(principal),
                           action="discovery.unignore", target_type="candidate",
                           target_id=candidate_id, ip=ip, user_agent=agent)
        await session.commit()
        return result
    except service.DiscoveryError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


class AcknowledgeRequest(BaseModel):
    candidate_ids: list[str] = Field(min_length=1, max_length=500)


class ScheduleRequest(BaseModel):
    name: str | None = None
    range_ids: list[str] = Field(min_length=1, max_length=128)
    interval_hours: int = 24


class ScheduleUpdate(BaseModel):
    name: str | None = None
    range_ids: list[str] | None = Field(default=None, min_length=1, max_length=128)
    interval_hours: int | None = None
    enabled: bool | None = None
    #: "Run it now" is a schedule whose next run is due immediately - the scheduler
    #: picks it up on its next tick and it then carries on at its interval.
    run_now: bool = False


@router.get("/missing", summary="Inventory the last sweep of its range did not hear")
async def missing(session: AsyncSession = Depends(get_session),
                  _: Principal = Depends(current_principal)) -> dict[str, Any]:
    """The mirror of "not in inventory": devices on record whose address a sweep
    covered and which did not answer it, with their polling state beside them -
    silent to the sweep but ONLINE to the poller is a credentials or ACL problem,
    not a dead box."""
    items = await service.missing(session)
    return {"items": items, "total": len(items)}


@router.post("/candidates/acknowledge",
             summary="Accept that what changed about these responders was expected")
async def acknowledge(body: AcknowledgeRequest, request: Request,
                      session: AsyncSession = Depends(get_session),
                      principal: Principal = Depends(require_role("operator")),
                      ) -> dict[str, Any]:
    """Audited per candidate. "The serial changed and somebody said that was fine"
    is exactly the line an investigation into a swapped box looks for."""
    actor = audit.actor_of(principal)
    ip, agent = audit.client_of(request)
    n = await service.acknowledge(session, body.candidate_ids, actor)
    for cid in body.candidate_ids:
        await audit.record(session, actor=actor, action="discovery.acknowledge",
                           target_type="candidate", target_id=cid,
                           ip=ip, user_agent=agent)
    await session.commit()
    return {"acknowledged": n}


@router.get("/schedules", summary="Ranges swept on a schedule")
async def list_schedules(session: AsyncSession = Depends(get_session),
                         _: Principal = Depends(current_principal)) -> dict[str, Any]:
    return {"items": await repo.list_schedules(session),
            "intervals": list(service.SCHEDULE_INTERVALS)}


@router.post("/schedules", status_code=status.HTTP_201_CREATED,
             summary="Sweep these ranges on an interval")
async def create_schedule(body: ScheduleRequest, request: Request,
                          session: AsyncSession = Depends(get_session),
                          principal: Principal = Depends(require_role("operator")),
                          ) -> dict[str, Any]:
    actor = audit.actor_of(principal)
    try:
        row = await service.create_schedule(
            session, name=body.name, range_ids=body.range_ids,
            interval_hours=body.interval_hours, actor=actor)
    except service.DiscoveryError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=actor, action="discovery.schedule.create",
                       target_type="discovery_schedule", target_id=row["id"],
                       ip=ip, user_agent=agent,
                       after={"range_ids": body.range_ids,
                              "interval_hours": row["interval_hours"]})
    await session.commit()
    return row


@router.patch("/schedules/{schedule_id}", summary="Change or pause a schedule")
async def update_schedule(schedule_id: str, body: ScheduleUpdate, request: Request,
                          session: AsyncSession = Depends(get_session),
                          principal: Principal = Depends(require_role("operator")),
                          ) -> dict[str, Any]:
    fields = body.model_dump(exclude_none=True, exclude={"run_now"})
    if body.run_now:
        from datetime import UTC, datetime
        fields["next_run_at"] = datetime.now(UTC).isoformat()
    try:
        row = await service.update_schedule(session, schedule_id, fields)
    except service.DiscoveryError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="discovery.schedule.update",
                       target_type="discovery_schedule", target_id=schedule_id,
                       ip=ip, user_agent=agent,
                       after=dict(fields))
    await session.commit()
    return row


@router.delete("/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Stop sweeping a range on a schedule")
async def delete_schedule(schedule_id: str, request: Request,
                          session: AsyncSession = Depends(get_session),
                          principal: Principal = Depends(require_role("operator")),
                          ) -> None:
    if not await repo.delete_schedule(session, schedule_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such schedule")
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="discovery.schedule.delete",
                       target_type="discovery_schedule", target_id=schedule_id,
                       ip=ip, user_agent=agent)
    await session.commit()


@router.post("/candidates/bulk-ignore", summary="Dismiss many responders")
async def bulk_ignore(body: BulkIgnoreRequest, request: Request,
                      session: AsyncSession = Depends(get_session),
                      principal: Principal = Depends(require_role("operator")),
                      ) -> dict[str, Any]:
    """One audit row PER candidate, not per batch.

    Dismissing forty responders is forty decisions about forty addresses, and "40
    candidates ignored" is not something anybody can act on six months later. Same
    rule the operator bulk lifecycle move follows.
    """
    actor = audit.actor_of(principal)
    ip, agent = audit.client_of(request)
    done, failed = [], []
    for cid in body.candidate_ids:
        try:
            await service.ignore(session, cid)
            await audit.record(session, actor=actor, action="discovery.ignore",
                               target_type="candidate", target_id=cid,
                               ip=ip, user_agent=agent)
            done.append(cid)
        except service.DiscoveryError as exc:
            failed.append({"id": cid, "error": str(exc)})
    await session.commit()
    log.info("bulk ignore", actor=actor, ignored=len(done), failed=len(failed))
    return {"ignored": len(done), "failed": failed}


@router.post("/candidates/bulk-promote",
             summary="Promote many responders, each under the name it reports")
async def bulk_promote(body: BulkPromoteRequest, request: Request,
                       session: AsyncSession = Depends(get_session),
                       principal: Principal = Depends(require_role("operator")),
                       ) -> dict[str, Any]:
    """Skips anything that does not name itself, and says which.

    A responder with no sysName or HostName cannot be promoted in bulk, because the
    alternative is inventing a name from its address - which would put IP addresses
    into the estate's naming scheme permanently. Those are reported back rather than
    silently dropped, so the operator knows to name them one at a time.
    """
    actor = audit.actor_of(principal)
    ip, agent = audit.client_of(request)
    promoted, skipped, failed = [], [], []
    for cid in body.candidate_ids:
        cand = await repo.get_candidate(session, cid)
        if cand is None:
            failed.append({"id": cid, "error": "no such candidate"})
            continue
        identity = cand.get("identity") or {}
        name = str(identity.get("hostName") or identity.get("sysName") or "").strip()
        if not name:
            skipped.append({"id": cid, "address": cand.get("address"),
                            "reason": "it does not report a name"})
            continue
        try:
            # Only what the sweep proved: a bulk promote cannot ask forty
            # operators for forty passwords, so an endpoint whose credential the
            # evidence cannot rebuild is reported back rather than guessed.
            result = await service.promote(
                session, cid,
                {"name": name, "device_type": body.device_type,
                 "auto_endpoints": body.auto_endpoints}, actor=actor)
            await audit.record(session, actor=actor, action="discovery.promote",
                               target_type="candidate", target_id=cid,
                               ip=ip, user_agent=agent,
                               after={"name": name, "bulk": True,
                                      "endpoints": len(result["endpoints"])})
            promoted.append(result)
        except service.DiscoveryError as exc:
            failed.append({"id": cid, "error": str(exc)})
    await session.commit()
    log.info("bulk promote", actor=actor, promoted=len(promoted),
             skipped=len(skipped), failed=len(failed))
    return {"promoted": promoted, "skipped": skipped, "failed": failed}


# ── meter channel schedules ──────────────────────────────────────────────────
#
# A branch-circuit monitor stores which breaker each CT is clamped to, written
# in at commissioning. Importing it is what lets a reading on channel 1 be
# attributed to the transfer switch it measures rather than being one of
# forty-two anonymous numbers.
#
# Under discovery because that is what it is: asking the equipment what it
# knows about itself. It is not a poll - a schedule changes when somebody
# moves a CT - so it runs on request, not on a timer.


@router.post("/meter-channels", summary="Import panel schedules from the meters")
async def import_meter_channels(
    request: Request,
    timeout: float = Query(2.0, ge=0.2, le=10.0,
                           description="Per-channel read timeout, seconds"),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    """Read every meter's channel descriptions and record the schedule.

    Synchronous, unlike a discovery sweep: this talks to meters already in the
    inventory on the management network, and an operator running it wants to
    see what it made of the labels - above all which ones it could not resolve.
    """
    result = await meter_commissioning.import_all(session, timeout=timeout)
    ip, agent = audit.client_of(request)
    # Who re-read the schedules, and what came back. A panel schedule decides
    # whose load a reading is, so a change to it is worth the same record as a
    # change to the wiring it describes.
    await audit.record(session, actor=audit.actor_of(principal),
                       action="meter_channels.import",
                       target_type="meter_channel", ip=ip, user_agent=agent,
                       after={"meters_read": result["meters_read"],
                              "channels": result["channels"],
                              "clamped": result["clamped"],
                              "unresolved": len(result["unresolved"])})
    await session.commit()
    return result


@router.get("/meter-channels", summary="What the meters said they measure")
async def meter_channel_stats(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return await meter_repo.stats(session)
