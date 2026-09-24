"""Discovery endpoints. Routing and validation only."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.logging import get_logger
from app.core.security import Principal, current_principal, require_role
from app.db.session import get_session
from app.repositories import discovery as repo
from app.repositories import meter_channels as meter_repo
from app.services import discovery as service
from app.services import meter_commissioning

log = get_logger("api.discovery")
router = APIRouter(prefix="/discovery", tags=["discovery"])


class RunRequest(BaseModel):
    method: str = "snmp_sweep"
    subnets: list[str] = Field(default_factory=list,
                               examples=[["10.51.0.0/24"]])


class PromoteRequest(BaseModel):
    name: str
    device_type: str | None = None
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
        run = await service.create_run(session, method=req.method,
                                       subnets=req.subnets)
        ip, agent = audit.client_of(request)
        # A discovery sweep is active traffic on the management network, sent
        # to addresses nobody has claimed yet. Who asked for it, and over which
        # subnets, is worth keeping.
        await audit.record(session, actor=audit.actor_of(principal),
                           action="discovery.run", target_type="discovery_run",
                           target_id=str(run.get("id")), ip=ip, user_agent=agent,
                           after={"method": req.method, "subnets": req.subnets})
        await session.commit()
        return run
    except service.DiscoveryError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.get("/subnets", summary="Management subnets worth sweeping")
async def suggest_subnets(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """The /24s the estate's management addresses already sit in.

    An operator should not have to know the site's addressing by heart to run an
    audit, and a free-text box invites both typos and a /16 - which the sweeper
    refuses outright rather than truncating, so the run just fails. These are
    derived from what inventory already holds, so a sweep of one of them is asking
    "is there anything here we do not know about" rather than guessing.
    """
    return {"subnets": await repo.mgmt_subnets(session)}


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
            result = await service.promote(
                session, cid,
                {"name": name, "device_type": body.device_type}, actor=actor)
            await audit.record(session, actor=actor, action="discovery.promote",
                               target_type="candidate", target_id=cid,
                               ip=ip, user_agent=agent,
                               after={"name": name, "bulk": True})
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
