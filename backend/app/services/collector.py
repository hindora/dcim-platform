"""Collector assignment service.

This is the one place in the system that decrypts device credentials and hands
them out. It is reachable only with a collector-scoped token, is scoped to the
requesting collector's shard, and every call is audit-logged - see docs/13
section B1 for why returning them at all is unavoidable.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import decrypt_secret
from app.repositories import collector as repo
from app.schemas import (
    Assignment,
    AssignmentCredential,
    AssignmentEndpoint,
    AssignmentPoll,
)
from app.services import sharding

log = get_logger("collector")


async def build_assignment(session: AsyncSession, collector_id: str,
                           protocols: list[str] | None = None) -> Assignment:
    rows = await repo.assignment_endpoints(session, collector_id, protocols)
    version = await repo.assignment_version(session, collector_id)

    # Shard BEFORE decrypting. The rows returned above are candidates - every
    # unpinned endpoint plus the ones pinned here - and handing all of them to
    # every collector is the overlap this phase exists to remove. Filtering
    # first also means a collector never decrypts a credential for a device it
    # does not own, which keeps the blast radius of a compromised collector to
    # its own shard rather than the fleet.
    # Registering before planning closes the first-fetch race described in
    # repositories/collector.register_collector.
    if await repo.register_collector(session, collector_id):
        log.info("collector registered on first assignment fetch",
                 collector_id=collector_id)
    collectors = [
        sharding.Collector(collector_id=c["collector_id"],
                           sites=frozenset(c["sites"]),
                           healthy=c["healthy"])
        for c in await repo.live_collectors(session)
    ]
    if not any(c.collector_id == collector_id for c in collectors):  # pragma: no cover
        # A collector asking for work before its first heartbeat has no row
        # yet. It still owns whatever the hash gives it, so it is added rather
        # than handed nothing - otherwise a fresh collector polls nothing until
        # its next heartbeat and the fleet is under-covered for that window.
        collectors.append(sharding.Collector(collector_id=collector_id))
    before = len(rows)
    rows = sharding.owned_by(rows, collectors, collector_id)
    log.info("assignment sharded", collector_id=collector_id,
             candidates=before, owned=len(rows), collectors=len(collectors))

    endpoints: list[AssignmentEndpoint] = []
    decrypt_failures = 0
    for r in rows:
        credential = None
        if r.get("secret_enc") is not None:
            try:
                credential = AssignmentCredential(
                    kind=r.get("credential_kind") or "none",
                    data=decrypt_secret(bytes(r["secret_enc"])),
                )
            except Exception:
                # A credential encrypted under a previous key must not take the
                # whole assignment down - the other endpoints still work.
                decrypt_failures += 1

        endpoints.append(AssignmentEndpoint(
            id=r["id"], device_id=r["device_id"], device_name=r["device_name"],
            device_type=r["device_type"], vendor=r.get("vendor"), model=r.get("model"),
            protocol=r["protocol"], role=r["role"],
            address=r.get("address"), port=r.get("port"),
            addressing=r.get("addressing") or {},
            via_endpoint_id=r.get("via_endpoint_id"),
            credential=credential,
            poll=AssignmentPoll(
                interval_s=r["interval_s"], timeout_ms=r["timeout_ms"],
                retries=r["retries"],
                metric_groups=list(r.get("metric_groups") or []),
                push_enabled=bool(r.get("push_enabled")),
            ),
        ))

    if decrypt_failures:
        log.error("credential decryption failed", count=decrypt_failures,
                  collector_id=collector_id)

    log.info("assignment served", collector_id=collector_id,
             endpoints=len(endpoints), version=version)

    return Assignment(version=version, generated_at=datetime.now(UTC),
                      collector_id=collector_id, endpoints=endpoints)


def etag_for(assignment: Assignment) -> str:
    """Weak ETag over the version plus the served content of each endpoint.

    Including the id set means a device removed from the fleet changes the ETag
    even if no timestamp moved.

    The poll settings are in here for a sharper reason. ``version`` is derived
    from ``device_endpoint.updated_at``, but ``interval_s`` and friends come
    from ``poll_profile``, which the endpoint rows only reference. Editing a
    profile - raising an interval across a whole class of devices, say - changes
    what this endpoint serves without touching a single endpoint row, so a
    version-only ETag answers 304 and every collector keeps polling at the old
    interval until something unrelated is edited or the process restarts.
    Digesting the poll fields makes the ETag track the body, which is what an
    ETag is for.
    """
    digest = hashlib.sha256()
    digest.update(str(assignment.version).encode())
    for e in assignment.endpoints:
        digest.update(e.id.encode())
        digest.update(f"|{e.address}|{e.port}|{e.poll.interval_s}"
                      f"|{e.poll.timeout_ms}|{e.poll.retries}"
                      f"|{e.poll.push_enabled}|{','.join(e.poll.metric_groups)}"
                      .encode())
    return f'W/"{digest.hexdigest()[:32]}"'
