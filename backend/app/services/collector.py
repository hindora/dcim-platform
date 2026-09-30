"""Collector assignment service.

This is the one place in the system that decrypts device credentials and hands
them out. It is reachable only with a collector-scoped token, is scoped to the
requesting collector's shard, and every call is audit-logged - see docs/13
section B1 for why returning them at all is unavoidable.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import decrypt_secret
from app.repositories import collector as repo
from app.repositories import pools as pool_repo
from app.schemas import (
    Assignment,
    AssignmentBBMD,
    AssignmentCredential,
    AssignmentEndpoint,
    AssignmentPoll,
    AssignmentPool,
    ResolveEntry,
)
from app.services import sealed_credential, sharding

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
    # A pending collector is not in the fleet, so it owns only what is
    # pinned to it - nothing, until an admin approves it.
    collectors = await fleet(session)
    before = len(rows)
    rows = sharding.owned_by(rows, collectors, collector_id)
    log.info("assignment sharded", collector_id=collector_id,
             candidates=before, owned=len(rows), collectors=len(collectors))

    # docs/26 Phase 4: a collector that has enrolled and registered an
    # encryption_pubkey gets every credential sealed to that key instead of
    # plaintext - see sealed_credential.py. Looked up once per call, not per
    # endpoint: it cannot change mid-request, and a collector with no key
    # yet (pre-Phase-4, or enrolled but never sent one) is unaffected.
    pubkey = await repo.encryption_pubkey(session, collector_id)

    endpoints: list[AssignmentEndpoint] = []
    decrypt_failures = 0
    seal_failures = 0
    for r in rows:
        credential = None
        if r.get("secret_enc") is not None:
            try:
                plain = decrypt_secret(bytes(r["secret_enc"]), key_id=r.get("credential_key_id"))
            except Exception:
                # A credential encrypted under a previous key must not take the
                # whole assignment down - the other endpoints still work. (Now
                # a real, named case: a row whose key_id names a ring entry
                # that has since been removed - see docs/26 Phase 4's
                # rotation script.)
                decrypt_failures += 1
            else:
                kind = r.get("credential_kind") or "none"
                cred_digest = hashlib.sha256(
                    json.dumps(plain, sort_keys=True, default=str).encode()).hexdigest()
                if pubkey:
                    try:
                        credential = AssignmentCredential(
                            kind=kind, digest=cred_digest,
                            sealed_b64=sealed_credential.seal_for_collector(
                                plain, pubkey))
                    except sealed_credential.SealError:
                        # A malformed stored pubkey must not silently fall
                        # back to plaintext - that would defeat the point of
                        # sealing the moment the stored key is ever corrupt.
                        # It drops this one endpoint's credential instead,
                        # same as a decrypt failure above.
                        seal_failures += 1
                else:
                    credential = AssignmentCredential(kind=kind, data=plain,
                                                       digest=cred_digest)

        endpoints.append(AssignmentEndpoint(
            id=r["id"], device_id=r["device_id"], device_name=r["device_name"],
            device_type=r["device_type"], vendor=r.get("vendor"), model=r.get("model"),
            protocol=r["protocol"], role=r["role"],
            address=r.get("address"), port=r.get("port"),
            addressing=r.get("addressing") or {},
            via_endpoint_id=r.get("via_endpoint_id"),
            pool_id=r.get("pool_id"),
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
    if seal_failures:
        log.error("credential sealing failed: stored encryption_pubkey is invalid",
                  count=seal_failures, collector_id=collector_id)

    log.info("assignment served", collector_id=collector_id,
             endpoints=len(endpoints), sealed=bool(pubkey), version=version)

    owned = {e.id for e in endpoints}
    resolve = await resolve_list(session, exclude=owned)
    me = next((c for c in collectors if c.collector_id == collector_id), None)
    site = me.sites if me else frozenset()

    # docs/26 Phase 5's pool-level settings, finally on the wire: every pool
    # an owned endpoint resolved into, plus the collector's own placement
    # pool (so a pool with a BBMD but no endpoints yet still registers).
    pool_ids = {e.pool_id for e in endpoints if e.pool_id}
    if me and me.pool_id:
        pool_ids.add(me.pool_id)
    pools: dict[str, AssignmentPool] = {}
    for p in await pool_repo.pools_by_ids(session, sorted(pool_ids)):
        bbmd = p.get("bbmd_settings") or {}
        pools[p["id"]] = AssignmentPool(
            id=p["id"], name=p["name"], site=p.get("site"), plane=p["plane"],
            trap_vip=p.get("trap_vip"),
            bbmd=AssignmentBBMD(enabled=bool(bbmd.get("enabled")),
                                bbmd=bbmd.get("bbmd"),
                                ttl_s=int(bbmd.get("ttl_s") or 300)),
            rate_budget_points_per_s=p.get("rate_budget_points_per_s"))

    return Assignment(version=version, generated_at=datetime.now(UTC),
                      collector_id=collector_id,
                      site=next(iter(site), None), endpoints=endpoints,
                      resolve=resolve, pools=pools)


async def fleet(session: AsyncSession) -> list[sharding.Collector]:
    """The collectors that share the fleet, as the sharding plan sees them.

    One definition, because four callers need it - the assignment, the shard
    summary, the visibility sweep and the ingest ownership check - and the
    first bug a second collector ever found here was two of them disagreeing
    about who could own what.
    """
    return [
        sharding.Collector(collector_id=c["collector_id"],
                           sites=frozenset(c["sites"]), pool_id=c.get("pool_id"),
                           healthy=c["healthy"], accepting=c["accepting"],
                           heartbeat_age_s=c.get("heartbeat_age_s"),
                           healthy_duration_s=c.get("healthy_duration_s"))
        for c in await repo.live_collectors(session)
    ]


async def ownership(session: AsyncSession) -> dict[str, str | None]:
    """Who owns every endpoint right now, pins included."""
    collectors = await fleet(session)
    if not collectors:
        return {}
    return sharding.plan(await repo.ownable_endpoints(session), collectors)


def community_digest(credential: dict[str, Any] | None) -> str | None:
    """sha256 of a v1/v2c community, the form a resolver may hold it in."""
    community = (credential or {}).get("community")
    if not isinstance(community, str) or not community:
        return None
    return hashlib.sha256(community.encode()).hexdigest()


async def resolve_list(session: AsyncSession,
                       exclude: set[str]) -> list[ResolveEntry]:
    """Every endpoint a trap could come from, minus the ones already owned.

    Estate-wide rather than site-wide, deliberately. A device sends its traps
    wherever it was configured to, and in the lab every datacenter sends to one
    receiver; on a real estate the same happens mid-migration, when a site's
    devices still point at the old collector. The resolver prefers its own
    shard and then its own site, and refuses an address that two other sites
    both use rather than guessing - overlapping RFC1918 between sites is
    common, and a wrong device is worse than no device.
    """
    out: list[ResolveEntry] = []
    failures = 0
    for r in await repo.resolvable_endpoints(session):
        if r["id"] in exclude:
            continue
        digest = None
        if r.get("secret_enc") is not None and r["protocol"] == "snmp":
            try:
                digest = community_digest(decrypt_secret(
                    bytes(r["secret_enc"]), key_id=r.get("credential_key_id")))
            except Exception:
                failures += 1
        out.append(ResolveEntry(
            id=r["id"], device_id=r["device_id"], device_name=r["device_name"],
            device_type=r["device_type"], protocol=r["protocol"], role=r["role"],
            address=r.get("address"), site=r.get("site"),
            community_sha256=digest))
    if failures:
        log.error("credential decryption failed building the resolve list",
                  count=failures)
    return out


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

    The credential is in here for the same reason. A password rotation writes
    the ``credential`` row and no endpoint row, so it used to answer 304 and a
    collector kept presenting the old password - failing every poll, and
    locking the account out on BMCs that count failures - until something
    unrelated moved an endpoint's timestamp.

    Digests ``credential.digest`` (sha256 of the plaintext, set by
    build_assignment), never ``credential.data`` or ``credential.sealed_b64``
    directly. docs/26 Phase 4 made those two mutually exclusive and neither is
    fit for this: ``data`` is empty once a credential is sealed, and
    ``sealed_b64`` is re-randomised - a fresh ephemeral key and nonce - on
    every single call, so it never matches even when nothing about the
    credential actually changed.
    """
    digest = hashlib.sha256()
    digest.update(str(assignment.version).encode())
    digest.update(f"|site|{assignment.site}".encode())
    for e in assignment.endpoints:
        digest.update(e.id.encode())
        digest.update(f"|{e.address}|{e.port}|{e.poll.interval_s}"
                      f"|{e.poll.timeout_ms}|{e.poll.retries}"
                      f"|{e.poll.push_enabled}|{','.join(e.poll.metric_groups)}"
                      .encode())
        if e.credential is not None:
            digest.update(b"|cred|")
            digest.update(e.credential.kind.encode())
            digest.update(e.credential.digest.encode())
    for r in assignment.resolve:
        digest.update(f"|r|{r.id}|{r.address}|{r.site}|{r.community_sha256}"
                      .encode())
    # Pool settings for the same reason as the poll profile above: a BBMD
    # or trap VIP edit writes collector_pool and no endpoint row, so a
    # version-only ETag would answer 304 and the collector would keep
    # registering with the old BBMD until something unrelated moved.
    for pool_id in sorted(assignment.pools):
        p = assignment.pools[pool_id]
        digest.update(f"|p|{pool_id}|{p.trap_vip}|{p.bbmd.enabled}|{p.bbmd.bbmd}"
                      f"|{p.bbmd.ttl_s}|{p.rate_budget_points_per_s}".encode())
    return f'W/"{digest.hexdigest()[:32]}"'
