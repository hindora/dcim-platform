"""Collector-facing endpoints.

Authenticated with a collector-scoped token, never a user JWT: the assignment
response contains decrypted device credentials.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from cryptography import x509
from cryptography.x509.oid import NameOID
from fastapi import (
    APIRouter,
    Body,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.security import (
    UNSCOPED_COLLECTOR,
    Principal,
    current_principal,
    forget_collector_cert,
    require_collector,
    require_collector_cert,
)
from app.db.session import get_session
from app.repositories import collector as repo
from app.repositories import collector_config as config_repo
from app.repositories import dashboard as dashboard_repo
from app.repositories import discovery as disc_repo
from app.repositories import preflight as preflight_repo
from app.schemas import Assignment, PreflightCheck, PreflightResult
from app.services import ca, collector_gateway, collector_pki
from app.services import collector as service
from app.services import discovery as disc_service

router = APIRouter(prefix="/collector", tags=["collector"])
log = get_logger("api.collector")


class EnrollRequest(BaseModel):
    token: str
    #: PEM-encoded PKCS#10 CSR, generated and signed by the collector's own,
    #: never-transmitted private key.
    csr_pem: str
    #: Base64 X25519 public key, collected now because enrollment is when the
    #: collector generates its keypair. Inert until Phase 4 seals credentials
    #: to it - stored, not yet read by anything.
    encryption_pubkey: str | None = None


class CertResponse(BaseModel):
    cert_pem: str
    #: Every certificate a client needs above its own, root last - append to
    #: cert_pem to get a complete verification chain in one PEM bundle.
    chain: list[str]
    not_after: datetime
    serial: str


@router.get("/trust-chain", summary="The CA chain this platform issues under")
async def trust_chain(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Public and unauthenticated - it is a list of certificates, the same
    information a browser gets from any TLS handshake against this platform.
    A fresh collector needs it before it can prove anything else, and the
    proxy in front of collector routes needs it to build its own client-CA
    trust bundle, so this is the one artifact both sides fetch rather than
    having copied onto them by hand at every site."""
    return {"chain": await collector_pki.trust_chain(session)}


@router.post("/enroll", response_model=CertResponse,
             summary="Exchange a one-time token and a CSR for a certificate")
async def enroll(
    body: EnrollRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CertResponse:
    """No collector or user auth: the one-time token IS the credential, and
    the whole point of enrollment is proving an identity that has no other
    credential yet."""
    ip, agent = audit.client_of(request)
    try:
        issued = await collector_pki.enroll(
            session, token=body.token, csr_pem=body.csr_pem,
            encryption_pubkey=body.encryption_pubkey)
    except (collector_pki.EnrollmentError, ca.CAError) as exc:
        await audit.record(session, actor="collector:unenrolled",
                           action="collector.enroll", ip=ip, user_agent=agent,
                           outcome="denied", after={"reason": str(exc)})
        await session.commit()
        log.warning("enrollment refused", error=str(exc), client=ip)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

    # Read back the collector id the CSR proved rather than trusting the
    # request body for it: sign_collector_csr already asserted the CSR's own
    # CN, so this is what the token was actually bound to.
    leaf = x509.load_pem_x509_certificate(issued.cert_pem.encode())
    collector_id = leaf.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value

    await audit.record(session, actor=f"collector:{collector_id}",
                       action="collector.enroll", target_type="collector",
                       target_id=collector_id, ip=ip, user_agent=agent,
                       after={"serial": issued.serial,
                              "not_after": issued.not_after.isoformat()})
    await session.commit()
    forget_collector_cert(collector_id)
    log.info("collector enrolled", collector_id=collector_id, serial=issued.serial,
             not_after=issued.not_after.isoformat(), client=ip)
    return CertResponse(cert_pem=issued.cert_pem,
                        chain=await collector_pki.trust_chain(session),
                        not_after=issued.not_after, serial=issued.serial)


@router.post("/renew", response_model=CertResponse,
             summary="Renew the current certificate before it expires")
async def renew(
    request: Request,
    csr_pem: str = Body(..., embed=True),
    session: AsyncSession = Depends(get_session),
    identity: str = Depends(require_collector_cert),
) -> CertResponse:
    """Requires the CURRENT certificate, not a bearer token - see
    ``require_collector_cert``. Authorization is holding that certificate;
    nothing here re-checks the CSR's key against the old one, because a
    collector renewing with a freshly generated key pair is a normal and
    even desirable rotation, not a red flag."""
    ip, agent = audit.client_of(request)
    try:
        issued = await collector_pki.renew(session, collector_id=identity,
                                           csr_pem=csr_pem)
    except (collector_pki.EnrollmentError, ca.CAError) as exc:
        await session.rollback()
        log.warning("renewal refused", collector_id=identity, error=str(exc))
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

    await audit.record(session, actor=f"collector:{identity}",
                       action="collector.renew", target_type="collector",
                       target_id=identity, ip=ip, user_agent=agent,
                       after={"serial": issued.serial,
                              "not_after": issued.not_after.isoformat()})
    await session.commit()
    forget_collector_cert(identity)
    log.info("collector certificate renewed", collector_id=identity,
             serial=issued.serial, not_after=issued.not_after.isoformat())
    return CertResponse(cert_pem=issued.cert_pem,
                        chain=await collector_pki.trust_chain(session),
                        not_after=issued.not_after, serial=issued.serial)


@router.post("/batches/{path_segment}",
             summary="Durable batch ingest - the WAN-safe path (docs/26 Phase 3)")
async def ingest_batch(
    path_segment: str,
    request: Request,
    x_dcim_spool_seq: int = Header(..., alias="X-DCIM-Spool-Seq"),
    settings: Settings = Depends(get_settings),
    identity: str = Depends(require_collector),
) -> dict[str, Any]:
    """Accept a zstd-compressed msgpack batch and XADD it under the
    identity this request actually authenticated as - never whatever the
    payload itself claims to be. See app/services/collector_gateway.py for
    what "durable" means here: 2xx is returned only once the XADD is
    confirmed, X-DCIM-Spool-Seq makes a retried POST idempotent rather than
    a duplicate, and the platform can answer 429 instead of falling over
    when Redis is under pressure.

    `identity` can be the UNSCOPED legacy token in a dev checkout with no
    mTLS proxy in front of it; a real remote-site collector authenticates by
    certificate the same as every other collector route since Phase 2.
    """
    if identity == UNSCOPED_COLLECTOR:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "the gateway needs a collector-scoped identity, "
                            "not the fleet-wide token")
    body = await request.body()
    redis = Redis.from_url(settings.redis_url)
    try:
        try:
            result = await collector_gateway.ingest_batch(
                redis, collector_id=identity, path_segment=path_segment,
                spool_seq=x_dcim_spool_seq, compressed_body=body)
        except collector_gateway.GatewayError as exc:
            headers = ({"Retry-After": str(exc.retry_after)}
                      if exc.retry_after else None)
            raise HTTPException(exc.status_code, str(exc), headers=headers) from None
    finally:
        await redis.aclose()
    return {"stream": result.stream, "accepted": result.accepted,
            "duplicate": result.duplicate, "entries": result.entries}


@router.get("/config", summary="Configuration this collector should run")
async def collector_config(
    response: Response,
    collector_id: str = Query(..., min_length=1, max_length=64),
    if_none_match: str | None = Header(None, alias="If-None-Match"),
    session: AsyncSession = Depends(get_session),
    identity: str = Depends(require_collector),
) -> dict[str, Any]:
    """The operational overrides, on the same collector token as assignments.

    Carries no secret - the token, the API address and Redis stay in the
    collector's own file - but it is scoped exactly like assignments anyway:
    a collector has no business reading what another one was told to run.

    The version is an integer rather than a hash so the collector can report
    back which one it is running. That is the difference between a settings
    page that shows what was saved and one that shows what is in force.
    """
    if identity != UNSCOPED_COLLECTOR and identity != collector_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "this token is not scoped to that collector")

    stored = await config_repo.get(session, collector_id)
    etag = f'W/"cfg-{stored["version"]}"'
    if if_none_match and if_none_match.strip() == etag:
        response.status_code = status.HTTP_304_NOT_MODIFIED
        return {}
    response.headers["ETag"] = etag
    return {"collector_id": collector_id, "version": stored["version"],
            "config": stored["config"]}


@router.get("/assignments", response_model=Assignment,
            summary="Endpoints this collector should poll")
async def assignments(
    request: Request,
    response: Response,
    collector_id: str = Query(..., min_length=1, max_length=64),
    protocol: list[str] | None = Query(None),
    if_none_match: str | None = Header(None, alias="If-None-Match"),
    session: AsyncSession = Depends(get_session),
    identity: str = Depends(require_collector),
):
    """The one endpoint that returns decrypted device credentials.

    It cannot be otherwise - the collector has to authenticate to devices - so
    the mitigations are what matter: a credential type no browser holds, a
    token bound to one collector, and an audit row for every handout.
    """
    ip, agent = audit.client_of(request)

    # Scope enforcement. A token derived for col-1 may not fetch col-2's
    # shard: collector_id arrives as a query parameter, so without this check
    # any holder of any collector token could ask for every other collector's
    # endpoints and be handed the credentials for all of them.
    if identity != UNSCOPED_COLLECTOR and identity != collector_id:
        await audit.record(
            session, actor=f"collector:{identity}", action="credential.denied",
            target_type="collector", target_id=collector_id, ip=ip,
            user_agent=agent, outcome="denied",
            after={"reason": "token is scoped to a different collector"})
        await session.commit()
        log.warning("assignment scope violation", token_identity=identity,
                    requested=collector_id, client=ip)
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "this token is not scoped to that collector")

    assignment = await service.build_assignment(session, collector_id, protocol)
    etag = service.etag_for(assignment)

    with_secrets = sum(1 for e in assignment.endpoints
                       if getattr(e, "credential", None) is not None)
    # Audit every fetch: this is the one endpoint that hands out secrets. The
    # row records how many credentials went out, never which - the count is
    # what an investigation needs, and the list would put the target set of a
    # compromise into a table that is easier to read than the one it protects.
    await audit.record(
        session, actor=f"collector:{identity}", action="credential.fetch",
        target_type="collector", target_id=collector_id, ip=ip,
        user_agent=agent,
        after={"endpoints": len(assignment.endpoints),
               "credentials_returned": with_secrets, "etag": etag,
               "scoped": identity != UNSCOPED_COLLECTOR})
    await session.commit()

    log.info("assignment fetch", collector_id=collector_id, identity=identity,
             client=ip, endpoints=len(assignment.endpoints),
             credentials=with_secrets, etag=etag,
             scoped=identity != UNSCOPED_COLLECTOR)
    if identity == UNSCOPED_COLLECTOR:
        # Visible rather than silent: a fleet-wide token is a standing risk,
        # and it should show up in the log every time it is used, not only in
        # a design document.
        log.warning("unscoped collector token used", collector_id=collector_id,
                    client=ip)

    if if_none_match and if_none_match.strip() == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED,
                        headers={"ETag": etag, "Cache-Control": "no-cache"})

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
    return assignment


@router.get("/instances", summary="Collector fleet health")
async def instances(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    return {"items": await dashboard_repo.collectors(session)}


@router.post("/heartbeat", status_code=status.HTTP_204_NO_CONTENT,
             summary="Heartbeat (fallback for collectors not using the stream)")
async def heartbeat(
    payload: dict,
    session: AsyncSession = Depends(get_session),
    identity: str = Depends(require_collector),
) -> Response:
    """Liveness for the collector the token names, and only that one.

    It used to trust the payload's ``collector_id``, so any collector token
    could keep any other collector looking alive - which is exactly the lie
    ``collector_stale`` exists to catch.
    """
    import json

    claimed = payload.get("collector_id")
    if identity != UNSCOPED_COLLECTOR:
        if claimed and claimed != identity:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "this token is not scoped to that collector")
        claimed = identity
    if not claimed:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "a heartbeat must name its collector")

    # A gateway-transport collector (docs/26 Phase 3) has no Redis heartbeat
    # stream to carry these on - it POSTs the same flat CollectorHeartbeat
    # shape here instead (contracts/schema/messages_v1.yaml's json tags match
    # these keys 1:1), so this fallback stores the identical stats shape
    # _handle_heartbeat builds from the Redis-delivered copy in
    # app/ingest/worker.py, keeping the two transports' visibility even.
    stats = dict(payload.get("stats") or {})
    for key in ("polls_total", "polls_failed", "traps_received", "events_received",
                "queue_depth", "queue_capacity", "assignment_age_s", "active_streams",
                "assignment_version", "mapping_bundle_sha", "config_version",
                "config_restart_pending", "config_error", "config_effective",
                "spool_bytes", "spool_oldest_age_s", "replay_rate"):
        if key in payload:
            stats[key] = payload[key]

    await repo.upsert_heartbeat(session, {
        "id": claimed,
        "version": payload.get("version"),
        "hostname": payload.get("hostname"),
        "started_at": payload.get("started_at"),
        "endpoints_owned": int(payload.get("endpoints_owned") or 0),
        "endpoints_online": int(payload.get("endpoints_online") or 0),
        "stats": json.dumps(stats),
    })
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def preflight_passed(checks: list[PreflightCheck]) -> bool:
    """A run passes only if every check that actually ran reported ok. A
    "skipped" check (no pool assigned yet to sample, say) does not count
    against it - it is neither a pass nor a fail - but "warn" does, since a
    passing wizard step should mean genuinely nothing left to fix, not
    "nothing FAILED"."""
    return all(c.status in ("ok", "skipped") for c in checks)


@router.get("/preflight-targets",
            summary="Sample device addresses for preflight reachability probes")
async def preflight_targets(
    collector_id: str = Query(..., min_length=1, max_length=64),
    session: AsyncSession = Depends(get_session),
    identity: str = Depends(require_collector),
) -> dict[str, Any]:
    """docs/26 Phase 8: where `dcim-collector preflight` should knock.

    Scoped exactly like /assignments - a token for col-1 may not map
    col-2's network - but credential-free, so unlike /assignments there is
    no secret handed out and nothing to audit per call. Addresses, ports
    and protocols only.
    """
    if identity != UNSCOPED_COLLECTOR and identity != collector_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "this token is not scoped to that collector")
    return await service.preflight_targets(session, collector_id)


@router.post("/preflight", status_code=status.HTTP_204_NO_CONTENT,
             summary="Record a preflight run (docs/26 Phase 8)")
async def preflight(
    body: PreflightResult,
    session: AsyncSession = Depends(get_session),
    identity: str = Depends(require_collector),
) -> Response:
    """`dcim-collector preflight`'s findings, stored for the onboarding
    wizard to poll - see app/repositories/preflight.py. Never blocks
    anything itself: a failed check is a finding for an operator to read,
    not a reason this platform refuses the collector that reported it -
    the same posture docs/26 Phase 7's version skew alarm takes, and for
    the same reason: this endpoint's job is to be honest, not to gate.

    An UNSCOPED_COLLECTOR (the legacy fleet-wide token, never issued after
    a real enroll) is refused - a preflight result with no real collector
    identity behind it is not useful to store, and every path that calls
    this (dcim-collector's own `preflight` command, run automatically
    after `enroll`) always has a scoped identity by the time it runs.
    """
    if identity == UNSCOPED_COLLECTOR:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "preflight needs a collector-scoped identity, "
                            "not the fleet-wide token")
    passed = preflight_passed(body.checks)
    await preflight_repo.record(session, identity, passed,
                               [c.model_dump() for c in body.checks])
    await session.commit()
    log.info("preflight recorded", collector_id=identity, passed=passed,
             checks=len(body.checks))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/discovery/claim",
            summary="Claim a pending discovery run (collector only)")
async def claim_discovery(
    collector_id: str | None = Query(None, max_length=128),
    features: str = Query("", max_length=256),
    session: AsyncSession = Depends(get_session),
    identity: str = Depends(require_collector),
) -> dict[str, Any]:
    """Hand one queued sweep the CALLER may do to it, or nothing.

    The sweep runs on the collector because that is what sits on the management
    network, so a run assigned to one collector must only ever reach that one.

    Who is asking: a scoped token says so, and a declared id that disagrees with
    it is refused. A legacy fleet-wide token cannot say, so the declared id is
    taken - the same id the collector heartbeats as, and a fleet token is
    trusted with every endpoint's credentials already. A collector that
    declares nothing claims only runs assigned to nobody.

    `features=exclude` is the collector saying it honours exclusions; without
    it, runs that carry any are never handed over.
    """
    scoped = identity != UNSCOPED_COLLECTOR
    if scoped and collector_id and collector_id != identity:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "declared collector id does not match the token")
    who = identity if scoped else (collector_id or None)
    supports = {f.strip() for f in features.split(",") if f.strip()}
    run = await disc_repo.claim_pending(session, collector_id=who,
                                        supports_exclude="exclude" in supports)
    # Committed immediately: the claim is the point. Without it the row's
    # status never leaves 'pending' and every collector claims it forever.
    await session.commit()
    return {"run": run}


class DiscoveryResult(BaseModel):
    address: str
    protocol: str = "snmp"
    identity: dict[str, Any] = Field(default_factory=dict)
    #: How the sweep reached it - port, scheme, which configured credential
    #: answered - by reference. A collector that predates it sends nothing.
    access: dict[str, str] = Field(default_factory=dict)


class DiscoveryResults(BaseModel):
    responders: list[DiscoveryResult] = Field(default_factory=list)
    error: str | None = None


@router.post("/discovery/{run_id}/results",
             summary="Report what a sweep found (collector only)")
async def discovery_results(run_id: str, body: DiscoveryResults,
                            session: AsyncSession = Depends(get_session),
                            identity: str = Depends(require_collector),
                            ) -> dict[str, Any]:
    # Only the collector that ran the sweep may report it. Results become
    # candidates and missing-device alarms, so one collector writing into
    # another's run is a way to raise alarms about a site it cannot see.
    if identity != UNSCOPED_COLLECTOR:
        owner = await disc_repo.run_claimant(session, run_id)
        if owner and owner != identity:
            log.warning("discovery results scope violation", run_id=run_id,
                        token_identity=identity, claimed_by=owner)
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "this sweep was claimed by another collector")
    # Only a run that is still running takes results. Locked, so a cancel and
    # this report cannot interleave. A cancelled run's sweep still finishes -
    # the collector cannot be interrupted - and what it found is discarded
    # rather than recorded against a run somebody stopped. A duplicate report
    # for a run already done is refused the same way.
    current = await disc_repo.lock_run_status(session, run_id)
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such discovery run")
    if current != "running":
        await session.commit()
        log.info("discovery results discarded", run_id=run_id, status=current,
                 responders=len(body.responders))
        return {"status": current, "discarded": len(body.responders)}
    if body.error:
        await disc_repo.finish_run(session, run_id, found=0, status="failed",
                                   error=body.error)
        await session.commit()
        return {"status": "failed"}
    result = await disc_service.record_results(
        session, run_id, [r.model_dump() for r in body.responders])
    # Straight away rather than on the next scheduler tick, so a 02:00 finding
    # is an alarm (and, by policy, a ticket) when the sweep lands. In a
    # savepoint: a failure here must not lose the results.
    try:
        async with session.begin_nested():
            from app.services import discovery_alarms
            await discovery_alarms.reconcile_and_enqueue(session)
    except Exception as exc:
        log.warning("discovery alarm reconcile failed", run_id=run_id, error=str(exc))
    await session.commit()
    return result


@router.get("/health", summary="Collector fleet and platform self-monitoring")
async def collector_health(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """What the platform currently believes about itself.

    Read-only: it gathers the same signals the worker's evaluator uses and
    reports the findings, without raising or clearing anything. The worker owns
    the alarm lifecycle - two writers would fight over the same rows, and the
    API has no business deciding the platform is unhealthy on a page load.

    The distinction this page exists to make: silence from the datacenter and
    silence from the monitoring look identical on every other screen.
    """
    from app.alarms import platform as rules
    from app.alarms import platform_monitor
    from app.contracts.messages_gen import Stream
    from app.repositories import alarms as alarm_repo

    redis = Redis.from_url(settings.redis_url)
    try:
        signals = await platform_monitor.gather(
            session, redis,
            streams=[Stream.TELEMETRY, Stream.EVENTS],
            group=settings.ingest_group)
    finally:
        await redis.aclose()

    findings = rules.evaluate(signals)
    open_alarms = await alarm_repo.open_platform_alarms(session)

    return {
        "verdict": rules.summarise(findings),
        "findings": [
            {"alarm_type": f.alarm_type, "instance": f.instance,
             "severity": f.severity, "message": f.message,
             "value": f.value, "threshold": f.threshold}
            for f in findings
        ],
        "open_alarms": open_alarms,
        "pipeline": {
            # Two numbers, never one. Freshness is bounded by the poll interval
            # even when everything is perfect; lag is publish-to-commit and is
            # sub-second when it is not broken.
            "ingest_lag_seconds": signals.ingest_lag_s,
            "telemetry_age_seconds": signals.telemetry_age_s,
            "telemetry_present": signals.telemetry_present,
            "worker_heartbeat_age_seconds": signals.worker_heartbeat_age_s,
            "stream_pending": signals.stream_pending,
            "lag_warning_seconds": rules.INGEST_LAG_WARNING_S,
            "lag_critical_seconds": rules.INGEST_LAG_CRITICAL_S,
        },
        # Who owns what. Without this the split is inferred rather than seen,
        # and the failure it hides is specific: a collector that is registered
        # but absent keeps its shard, so its endpoints are polled by nobody -
        # and from every other collector's point of view they are simply "not
        # mine". collector_stale says a collector is gone; this says how much
        # of the fleet went with it.
        "shards": await _shard_summary(session),
        "collectors": [
            {"collector_id": c.collector_id,
             "heartbeat_age_seconds": c.heartbeat_age_s,
             "status": c.status,
             "endpoints_owned": c.endpoints_owned,
             "endpoints_online": c.endpoints_online,
             "stale_after_seconds": rules.COLLECTOR_STALE_S}
            for c in signals.collectors
        ],
    }


async def _shard_summary(session: AsyncSession) -> dict[str, Any]:
    """Endpoints per collector, plus anything nothing can reach.

    From the whole fleet's plan. It used to plan over the FIRST collector's
    candidates, which leave out every endpoint pinned to any other collector -
    so a pin made a collector's shard look smaller than it is on exactly the
    page used to check that pins took.
    """
    from app.services import sharding

    collectors = await service.fleet(session)
    if not collectors:
        return {"collectors": 0, "owned": {}, "unassigned": None,
                "note": "no collector has ever registered, so nothing is assigned"}

    plan = await service.ownership(session)
    counts = sharding.distribution(plan)
    healthy = {c.collector_id for c in collectors if c.healthy}
    stranded = sum(n for cid, n in counts.items()
                   if cid != "(unassigned)" and cid not in healthy)
    return {
        "collectors": len(collectors),
        "owned": {k: v for k, v in counts.items() if k != "(unassigned)"},
        "unassigned": counts.get("(unassigned)", 0),
        # The number that matters during an incident: endpoints owned by a
        # collector that is not currently answering.
        "owned_by_unhealthy": stranded,
    }
