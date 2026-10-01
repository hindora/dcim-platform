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

from sqlalchemy import text
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
from app.services import sealed_credential, sharding, target_limits

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
    before = len(rows)
    # The recorded owner where it is valid, the live plan where it is not -
    # the same answer the ingest ownership check and every page read, from
    # ownership() below. Serving the live plan alone skipped the assigner's
    # damping and HA failover entirely.
    collectors = await fleet(session)
    if collectors:
        owners = await ownership(session)
        rows = [r for r in rows if owners.get(str(r["id"])) == collector_id]
    else:
        # No fleet yet (the first collector, still pending): only what is
        # pinned to it, exactly as before.
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
    pool_rows = await pool_repo.pools_by_ids(session, sorted(pool_ids))
    # Per-address limits: the endpoint's own override, else its pool's
    # default for the protocol - resolved here so a collector applies one
    # value and never needs the rule.
    defaults = {p["id"]: p.get("target_limits") or {} for p in pool_rows}
    overrides = {str(r["id"]): r.get("target_limit") for r in rows}
    for e in endpoints:
        e.target_limit = target_limits.resolve(
            overrides.get(e.id), defaults.get(e.pool_id or ""), e.protocol)
    # Each collector enforces its share of a pool budget, from ownership:
    # the same owners map the serving path used above.
    budgets = {p["id"]: p.get("rate_budget_points_per_s") for p in pool_rows}
    shares: dict[str, float] = {}
    if collectors and any(budgets.values()):
        pool_of = {str(r["id"]): r.get("pool_id")
                   for r in await repo.ownable_endpoints(session)}
        shares = target_limits.budget_shares(budgets, owners, pool_of, collector_id)
    pools: dict[str, AssignmentPool] = {}
    for p in pool_rows:
        bbmd = p.get("bbmd_settings") or {}
        pools[p["id"]] = AssignmentPool(
            id=p["id"], name=p["name"], site=p.get("site"), plane=p["plane"],
            trap_vip=p.get("trap_vip"),
            bbmd=AssignmentBBMD(enabled=bool(bbmd.get("enabled")),
                                bbmd=bbmd.get("bbmd"),
                                ttl_s=int(bbmd.get("ttl_s") or 300)),
            rate_budget_points_per_s=p.get("rate_budget_points_per_s"),
            rate_budget_share_points_per_s=shares.get(p["id"]))

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
    """Who owns every endpoint right now: pins, then the assigner's record
    where it is still valid, then the live plan (sharding.effective).

    The one answer every reader shares - the serving path, the ingest
    ownership check, the visibility sweep, the pools and shard pages. Two
    of them disagreeing is how a collector's own telemetry gets dropped as
    coming from a non-owner."""
    collectors = await fleet(session)
    if not collectors:
        return {}
    return sharding.effective(await repo.ownable_endpoints(session), collectors,
                              await repo.current_assignment(session))


#: Heartbeat stats a collector detail page shows, in the order it shows
#: them. A key the collector never sent stays None rather than 0: a
#: redis-transport collector has no spool at all, and "0 bytes spooled"
#: would claim a measurement nobody took.
DETAIL_STATS = ("polls_total", "polls_failed", "traps_received", "events_received",
                "queue_depth", "queue_capacity", "assignment_age_s", "assignment_version",
                "active_streams", "spool_bytes", "spool_oldest_age_s", "replay_rate",
                "mapping_bundle_sha")


def summarise_detail(collector_id: str, endpoints: list[dict[str, Any]],
                     stats: dict[str, Any] | None, recent: int = 20) -> dict[str, Any]:
    """Pure: the counts and lists a collector detail page is built from.

    `reported_elsewhere` is the one number here that is not a count of the
    collector's own state: endpoints the PLAN gives this collector whose
    last report came from a different one. Non-zero during a move, a
    drain or a failover - and stuck non-zero means the move never happened.
    """
    by_status: dict[str, int] = {}
    by_protocol: dict[str, int] = {}
    elsewhere = 0
    for e in endpoints:
        by_status[e["status"]] = by_status.get(e["status"], 0) + 1
        by_protocol[e["protocol"]] = by_protocol.get(e["protocol"], 0) + 1
        if e.get("reported_by") and e["reported_by"] != collector_id:
            elsewhere += 1
    errors = sorted((e for e in endpoints if e.get("last_error")),
                    key=lambda e: str(e.get("last_failure") or ""), reverse=True)
    s = stats or {}
    queue = None
    if s.get("queue_depth") is not None and s.get("queue_capacity"):
        queue = round(100.0 * float(s["queue_depth"]) / float(s["queue_capacity"]), 1)
    return {
        "by_status": dict(sorted(by_status.items())),
        "by_protocol": dict(sorted(by_protocol.items())),
        "reported_elsewhere": elsewhere,
        "recent_errors": errors[:recent],
        "error_count": len(errors),
        "stats": {k: s.get(k) for k in DETAIL_STATS},
        # Publish-queue fill - how far behind sending it is. Not poll
        # capacity, which is `capacity` below.
        "queue_fill_pct": queue,
        # docs/26 Phase 5: the collector's own poll-worker capacity report
        # over its trailing window, as sent. None until it has sent one.
        "capacity": s.get("capacity") if isinstance(s.get("capacity"), dict) else None,
    }


async def collector_detail(session: AsyncSession, collector_id: str) -> dict[str, Any] | None:
    from app import __version__ as platform_version
    from app.repositories import collector_config as config_repo
    from app.repositories import preflight as preflight_repo
    from app.services import version_skew

    row = next((r for r in await config_repo.list_collectors(session)
                if r["id"] == collector_id), None)
    if row is None:
        return None
    owned = [eid for eid, owner in (await ownership(session)).items()
             if owner == collector_id]
    endpoints = await repo.endpoint_health(session, owned)
    stats = (await session.execute(text(
        "SELECT stats FROM collector_instance WHERE id = :id"), {"id": collector_id})).scalar()
    out = {**row, **summarise_detail(collector_id, endpoints, stats)}
    # config/effective are the settings page's business, not this one's.
    out.pop("config", None)
    out.pop("effective", None)
    out["platform_version"] = platform_version
    out["version_skew"] = version_skew.classify(platform_version, row.get("build"))
    out["endpoints"] = endpoints
    out["preflight"] = await preflight_repo.latest(session, collector_id)
    return out


#: Protocols a preflight reachability probe means nothing for: both are
#: INBOUND to the collector, so "can the collector reach the device" is
#: the wrong question - the trap_port check already asks the right one.
_INBOUND_ONLY = frozenset({"snmp_trap", "sflow"})


def sample_targets(rows: list[dict[str, Any]],
                   per_protocol: int = 3) -> list[dict[str, Any]]:
    """Pure: up to `per_protocol` distinct (address, port) per protocol.

    Real device addresses rather than hosts picked from a CIDR: a random
    address in 10.52.1.0/24 answering nothing proves nothing, while a known
    chiller's controller not answering is exactly the facilities pinhole
    that is not in yet. Distinct, because every field device behind one
    Modbus gateway shares the gateway's address - probing it eighteen times
    is one probe with extra steps. Sorted, so the same fleet always yields
    the same sample and a rerun compares like with like.
    """
    from app.services.endpoint_config import DEFAULT_PORT

    by_proto: dict[str, set[tuple[str, int]]] = {}
    for r in rows:
        proto = r["protocol"]
        if proto in _INBOUND_ONLY or not r.get("address"):
            continue
        port = r.get("port") or DEFAULT_PORT.get(proto)
        if not port:
            continue
        by_proto.setdefault(proto, set()).add((r["address"], int(port)))
    out = []
    for proto in sorted(by_proto):
        picked = sorted(by_proto[proto])[:per_protocol]
        out.append({"protocol": proto,
                    "targets": [{"address": a, "port": p} for a, p in picked],
                    "total": len(by_proto[proto])})
    return out


async def preflight_targets(session: AsyncSession, collector_id: str) -> dict[str, Any]:
    """What `dcim-collector preflight` should try to reach (docs/26 Phase 8).

    The endpoints this collector owns right now; or, for a collector that
    owns nothing yet - pending, or just enrolled, the exact moment preflight
    runs - every endpoint in the pool it is placed in, which is what it WILL
    own. With neither there is nothing honest to probe, and saying so beats
    inventing a target.
    """
    owned = [eid for eid, owner in (await ownership(session)).items()
             if owner == collector_id]
    rows = await pool_repo.probe_rows(session, endpoint_ids=owned) if owned else []
    source = "owned"
    if not rows:
        state = await repo.collector_state(session, collector_id)
        pool_id = (state or {}).get("pool_id")
        if pool_id:
            rows = await pool_repo.probe_rows(session, pool_id=pool_id)
            source = "pool"
    if not rows:
        source = "none"
    return {"collector_id": collector_id, "source": source,
            "protocols": sample_targets(rows)}


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
                      f"|{sorted((e.target_limit or {}).items())}"
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
                      f"|{p.bbmd.ttl_s}|{p.rate_budget_points_per_s}"
                      f"|{p.rate_budget_share_points_per_s}".encode())
    return f'W/"{digest.hexdigest()[:32]}"'
