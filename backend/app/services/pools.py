"""Collector pools: site x plane placement, and what a pool implies.

docs/26 Phase 5 put the table in (migration 0088) and the assigner has read
it since; this is the operator-facing half - validation, the aggregated view
a pool page needs (members, owned, unassigned, protocols in play), and the
per-pool firewall matrix the deploy row of that phase's table asked for.

The firewall matrix is the part worth reading closely. It is derived, not
authored: from the discovery ranges whose endpoints resolve into the pool,
the protocols actually present on those endpoints, and the pool's own
settings (trap VIP, BBMD). A matrix somebody typed by hand is out of date
the first time a range is added; one derived from the same rows the
assigner shards cannot be.
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import collector as fleet_repo
from app.repositories import pools as repo
from app.repositories import preflight as preflight_repo
from app.services import collector as fleet
from app.services.discovery_ranges import PURPOSES as PLANES
from app.services.endpoint_config import DEFAULT_PORT


class PoolError(ValueError):
    """A value the operator can fix; the message is shown as-is."""


class PoolConflictError(PoolError):
    """A pool for that site and plane already exists."""


class PoolNotFoundError(PoolError):
    pass


# --------------------------------------------------------------- validation

def validate_bbmd(raw: Any) -> dict[str, Any]:
    """The pool's BACnet Foreign Device Registration settings.

    ``{}`` is "static unicast, no BBMD" - docs/26 Phase 9's own default, and
    the value migration 0088 gave every row. Enabled, it needs a BBMD to
    register with; the collector's fdr.go renews at half the TTL, so a TTL
    is a real operational number rather than decoration. Kept as a plain
    dict rather than a model because it lands in a JSONB column and travels
    to the collector as JSON - one shape, three consumers.
    """
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise PoolError("bbmd_settings must be an object")
    unknown = set(raw) - {"enabled", "bbmd", "ttl_s"}
    if unknown:
        raise PoolError(f"bbmd_settings: unknown keys {sorted(unknown)}")
    enabled = bool(raw.get("enabled", False))
    bbmd = raw.get("bbmd")
    ttl = raw.get("ttl_s", 300)
    if not enabled:
        return {"enabled": False, **({"bbmd": bbmd} if bbmd else {}), "ttl_s": int(ttl or 300)}
    if not isinstance(bbmd, str) or ":" not in bbmd:
        raise PoolError("bbmd_settings.bbmd must be host:port when enabled")
    host, _, port = bbmd.rpartition(":")
    if not host or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise PoolError("bbmd_settings.bbmd must be host:port when enabled")
    try:
        ttl = int(ttl)
    except (TypeError, ValueError):
        raise PoolError("bbmd_settings.ttl_s must be an integer") from None
    if not 10 <= ttl <= 65535:
        # 16-bit on the wire (Annex J.5.2's Register-Foreign-Device carries
        # a 2-octet TTL); under ten seconds is a renewal storm, not a setting.
        raise PoolError("bbmd_settings.ttl_s must be between 10 and 65535")
    return {"enabled": True, "bbmd": bbmd, "ttl_s": ttl}


def _cidrs(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PoolError("cidrs must be a list")
    out: list[str] = []
    for item in raw:
        try:
            net = ipaddress.ip_network(str(item).strip(), strict=False)
        except ValueError:
            raise PoolError(f"cidrs: {item!r} is not a CIDR") from None
        out.append(str(net))
    return out


def _trap_vip(raw: Any) -> str | None:
    if raw in (None, ""):
        return None
    try:
        return str(ipaddress.ip_address(str(raw).strip()))
    except ValueError:
        raise PoolError(f"trap_vip: {raw!r} is not an IP address") from None


def clean(payload: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    """Validate a create (every required key) or a patch (only what is sent)."""
    out: dict[str, Any] = {}
    if "name" in payload or not partial:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise PoolError("name is required")
        if len(name) > 64:
            raise PoolError("name must be 64 characters or fewer")
        out["name"] = name
    if not partial:
        if not payload.get("datacenter_id"):
            raise PoolError("datacenter_id is required")
        out["datacenter_id"] = str(payload["datacenter_id"])
        plane = payload.get("plane")
        if plane not in PLANES:
            raise PoolError(f"plane must be one of {', '.join(PLANES)}")
        out["plane"] = plane
    elif "datacenter_id" in payload or "plane" in payload:
        # Site and plane are the pool's identity - the (datacenter, plane)
        # unique key, what every endpoint resolves against. Moving them
        # would silently re-home every endpoint in the pool; that is a new
        # pool, not an edit.
        raise PoolError("datacenter_id and plane cannot be changed; create a new pool")
    if "cidrs" in payload or not partial:
        out["cidrs"] = _cidrs(payload.get("cidrs"))
    if "trap_vip" in payload or not partial:
        out["trap_vip"] = _trap_vip(payload.get("trap_vip"))
    if "bbmd_settings" in payload or not partial:
        out["bbmd_settings"] = validate_bbmd(payload.get("bbmd_settings"))
    if "rate_budget_points_per_s" in payload or not partial:
        rb = payload.get("rate_budget_points_per_s")
        if rb is not None:
            try:
                rb = int(rb)
            except (TypeError, ValueError):
                raise PoolError("rate_budget_points_per_s must be an integer") from None
            if rb <= 0:
                raise PoolError("rate_budget_points_per_s must be positive")
        out["rate_budget_points_per_s"] = rb
    if "min_members" in payload or not partial:
        mm = payload.get("min_members", 1)
        try:
            mm = int(mm if mm is not None else 1)
        except (TypeError, ValueError):
            raise PoolError("min_members must be an integer") from None
        if mm < 1:
            raise PoolError("min_members must be at least 1")
        out["min_members"] = mm
    return out


# ----------------------------------------------------------------- reading

def aggregate(pools: list[dict[str, Any]], members: list[dict[str, Any]],
              endpoint_counts: list[dict[str, Any]],
              ownable: list[dict[str, Any]],
              plan: dict[str, str | None]) -> dict[str, Any]:
    """Pure: join pools with their members, endpoint totals and the plan.

    Separated from the queries so the arithmetic - which is where a page
    like this goes wrong - is testable against fixed rows.
    """
    by_pool: dict[str, dict[str, Any]] = {}
    for p in pools:
        by_pool[p["id"]] = {**p, "members": [], "healthy_members": 0,
                            "accepting_members": 0, "endpoints": 0,
                            "protocols": {}, "unassigned": 0, "owned": 0}
    for m in members:
        p = by_pool.get(m["pool_id"])
        if p is None:
            continue
        p["members"].append(m)
        p["healthy_members"] += int(bool(m["healthy"]))
        p["accepting_members"] += int(bool(m["healthy"] and m["accepting"]))
    unpooled_protocols: dict[str, int] = {}
    for c in endpoint_counts:
        p = by_pool.get(c["pool_id"] or "")
        if p is None:
            unpooled_protocols[c["protocol"]] = (
                unpooled_protocols.get(c["protocol"], 0) + int(c["n"]))
            continue
        p["endpoints"] += int(c["n"])
        p["protocols"][c["protocol"]] = p["protocols"].get(c["protocol"], 0) + int(c["n"])
    unpooled_unassigned = 0
    for e in ownable:
        owner = plan.get(e["id"])
        p = by_pool.get(e.get("pool_id") or "")
        if p is None:
            unpooled_unassigned += int(owner is None)
            continue
        if owner is None:
            p["unassigned"] += 1
        else:
            p["owned"] += 1
    for p in by_pool.values():
        p["members"].sort(key=lambda m: m["collector_id"])
        p["below_min_members"] = (p["min_members"] > 1
                                  and p["accepting_members"] < p["min_members"])
    return {
        "pools": sorted(by_pool.values(), key=lambda p: (p["site"], p["plane"])),
        # Endpoints that resolve to no pool at all: a site with ranges but
        # no pool for that plane yet, or an endpoint outside every range.
        # Served by unplaced collectors, if any, exactly as before pools.
        "unpooled": {"endpoints": sum(unpooled_protocols.values()),
                     "protocols": unpooled_protocols,
                     "unassigned": unpooled_unassigned},
    }


async def overview(session: AsyncSession) -> dict[str, Any]:
    pools = await repo.list_pools(session)
    members = await repo.members(session)
    counts = await repo.endpoint_counts(session)
    ownable = await fleet_repo.ownable_endpoints(session)
    plan = await fleet.ownership(session)
    out = aggregate(pools, members, counts, ownable, plan)
    out["planes"] = list(PLANES)
    return out


async def detail(session: AsyncSession, pool_id: str) -> dict[str, Any]:
    pool = await repo.get_pool(session, pool_id)
    if pool is None:
        raise PoolNotFoundError(pool_id)
    members = [m for m in await repo.members(session) if m["pool_id"] == pool_id]
    counts = [c for c in await repo.endpoint_counts(session) if c["pool_id"] == pool_id]
    ownable = [e for e in await fleet_repo.ownable_endpoints(session)
               if e.get("pool_id") == pool_id]
    plan = await fleet.ownership(session)
    agg = aggregate([pool], members, counts, ownable, plan)["pools"][0]
    agg["ranges"] = await repo.ranges_for(session, pool_id)
    return agg


# ------------------------------------------------------------- readiness

#: Protocols that cannot be polled without a secret. BACnet/IP and
#: Modbus/TCP carry no authentication at all (BACnet/SC aside, which nothing
#: here speaks), so an endpoint of theirs with no credential is correct, not
#: a gap - counting it as one would make every BMS pool look unfinished.
CREDENTIALED = frozenset({"snmp", "redfish", "gnmi", "provider"})


def summarise_readiness(pool: dict[str, Any], protocols: list[dict[str, Any]],
                        preflights: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """Pure: is this pool actually collecting, and if not, which step of
    bringing it up is unfinished (docs/26 Phase 8's wizard, steps 6-10).

    Each check is ok True / False, or None when there is nothing to judge
    yet - a pool with no endpoints is not "all online", and saying so
    would turn the wizard's last step green on an empty site."""
    protos = []
    for r in protocols:
        needs = r["protocol"] in CREDENTIALED
        gap = r["endpoints"] - r["with_credential"] if needs else 0
        protos.append({**r, "needs_credential": needs, "missing_credential": gap})
    total = sum(r["endpoints"] for r in protos)
    online = sum(r["online"] for r in protos)
    missing = sum(r["missing_credential"] for r in protos)

    members = []
    for m in pool.get("members", []):
        pf = preflights.get(m["collector_id"])
        members.append({"collector_id": m["collector_id"], "state": m["state"],
                        "has_run": bool(m.get("has_run", True)),
                        "healthy": bool(m["healthy"]) and bool(m.get("has_run", True)),
                        "accepting": bool(m["accepting"]),
                        "preflight_passed": None if pf is None else bool(pf["passed"]),
                        "preflight_at": None if pf is None else pf["ran_at"]})

    need = max(1, int(pool.get("min_members") or 1))
    accepting = int(pool.get("accepting_members") or 0)
    ran = [m for m in members if m["preflight_passed"] is not None]
    checks = [
        {"key": "members", "ok": accepting >= need,
         "detail": f"{accepting} of {need} required member(s) healthy and accepting work"},
        {"key": "preflight",
         "ok": None if not members else (len(ran) == len(members)
                                         and all(m["preflight_passed"] for m in ran)),
         "detail": ("no collector placed" if not members else
                    f"{sum(1 for m in ran if m['preflight_passed'])} of {len(members)} "
                    "member(s) passed their latest preflight")},
        {"key": "endpoints", "ok": None if total == 0 else True,
         "detail": (f"{total} endpoint(s) resolve here" if total else
                    "no endpoint resolves here - add a discovery range with this "
                    "site and plane, then discover and promote")},
        {"key": "credentials", "ok": None if total == 0 else missing == 0,
         "detail": ("nothing to check yet" if total == 0 else
                    f"{missing} endpoint(s) of a credentialed protocol have no credential"
                    if missing else "every endpoint that needs a credential has one")},
        {"key": "assigned", "ok": None if total == 0 else int(pool.get("unassigned") or 0) == 0,
         "detail": ("nothing to check yet" if total == 0 else
                    f"{int(pool.get('unassigned') or 0)} endpoint(s) owned by nobody")},
        {"key": "online", "ok": None if total == 0 else online == total,
         "detail": ("nothing to check yet" if total == 0 else
                    f"{online} of {total} endpoint(s) online at the last poll")},
    ]
    return {"pool_id": pool["id"], "protocols": protos, "members": members,
            "checks": checks,
            "ready": all(c["ok"] is True for c in checks)}


async def readiness(session: AsyncSession, pool_id: str) -> dict[str, Any]:
    agg = await detail(session, pool_id)
    protos = await repo.readiness_counts(session, pool_id)
    preflights = {m["collector_id"]: await preflight_repo.latest(session, m["collector_id"])
                  for m in agg["members"]}
    out = summarise_readiness(agg, protos, preflights)
    out["ranges"] = agg["ranges"]
    return out


# ----------------------------------------------------------------- writing

async def create(session: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    values = clean(payload, partial=False)
    try:
        async with session.begin_nested():
            pool_id = await repo.create_pool(session, values)
    except IntegrityError as exc:
        # One pool per site and plane: that key is how an endpoint resolves
        # to a pool at all, so a second one would make resolution ambiguous.
        if "collector_pool_site_plane" in str(exc.orig):
            raise PoolConflictError("a pool already exists for that site and plane") from None
        raise PoolError("no such site") from None
    pool = await repo.get_pool(session, pool_id)
    assert pool is not None
    return pool


async def update(session: AsyncSession, pool_id: str,
                 changes: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    current = await repo.get_pool(session, pool_id)
    if current is None:
        raise PoolNotFoundError(pool_id)
    values = clean(changes, partial=True)
    effective = {k: v for k, v in values.items() if current.get(k) != v}
    if not effective:
        return {}, {}
    await repo.update_pool(session, pool_id, effective)
    return {k: current.get(k) for k in effective}, effective


async def delete(session: AsyncSession, pool_id: str) -> None:
    if await repo.get_pool(session, pool_id) is None:
        raise PoolNotFoundError(pool_id)
    placed = await repo.collector_count(session, pool_id)
    if placed:
        # The FK is ON DELETE SET NULL, so the delete would succeed and
        # silently turn every member back into an unplaced "serves
        # everything" collector - a placement change nobody asked for.
        raise PoolError(f"{placed} collector(s) are placed in this pool; move them first")
    await repo.delete_pool(session, pool_id)


# ---------------------------------------------------------- firewall matrix

_UDP = frozenset({"snmp", "snmp_trap", "bacnet", "sflow"})
#: Inbound listeners a collector opens, by the protocol whose presence
#: implies them - the ports docs/26's operator journey names for the
#: network team's change request (step 2).
_TRAP_PORT = 162
_REDFISH_EVENT_PORT = 9143
_CORE_PORT = 443


def _transport(protocol: str) -> str:
    return "udp" if protocol in _UDP else "tcp"


def firewall_matrix(pool: dict[str, Any], ranges: list[dict[str, Any]],
                    protocols: dict[str, int], core_url: str | None) -> dict[str, Any]:
    """Pure. One row per (direction, source, destination, port) a pool needs.

    Sources and destinations are named, never guessed: device-side rules
    target the discovery ranges that actually resolve into this pool (plus
    the pool's own extra CIDRs), and the collector side is "the collectors
    placed in this pool" - whichever those are on the day the rule is
    applied, which is the right granularity for a firewall rule that must
    outlive any one VM.
    """
    dests = [r["cidr"] for r in ranges if r.get("enabled", True)]
    dests += [c for c in pool.get("cidrs") or [] if c not in dests]
    collectors = f"collectors in pool {pool['name']}"
    rows: list[dict[str, Any]] = []

    core_host = "<the DCIM platform>"
    if core_url:
        core_host = urlsplit(core_url).hostname or core_url
    rows.append({"direction": "outbound", "from": collectors, "to": core_host,
                 "transport": "tcp", "port": _CORE_PORT, "protocol": "core",
                 "why": "assignment, config, credential and telemetry path to the core"})

    for protocol in sorted(protocols):
        if protocols[protocol] <= 0:
            continue
        if protocol == "snmp_trap":
            continue  # inbound; handled below
        if protocol == "provider":
            rows.append({"direction": "outbound", "from": collectors,
                         "to": "the provider's API (each endpoint's address)",
                         "transport": "tcp", "port": _CORE_PORT, "protocol": protocol,
                         "why": f"{protocols[protocol]} provider endpoint(s)"})
            continue
        port = DEFAULT_PORT.get(protocol)
        if port is None:
            continue
        for dest in dests:
            rows.append({"direction": "outbound", "from": collectors, "to": dest,
                         "transport": _transport(protocol), "port": port,
                         "protocol": protocol,
                         "why": f"{protocols[protocol]} {protocol} endpoint(s)"})

    trap_to = pool.get("trap_vip") or collectors
    if dests and ("snmp" in protocols or "snmp_trap" in protocols):
        for dest in dests:
            rows.append({"direction": "inbound", "from": dest, "to": trap_to,
                         "transport": "udp", "port": _TRAP_PORT, "protocol": "snmp_trap",
                         "why": ("SNMP traps to the pool's trap VIP" if pool.get("trap_vip")
                                 else "SNMP traps (no trap VIP set: each collector's "
                                      "own address)")})
    if dests and protocols.get("redfish"):
        for dest in dests:
            rows.append({"direction": "inbound", "from": dest, "to": collectors,
                         "transport": "tcp", "port": _REDFISH_EVENT_PORT,
                         "protocol": "redfish_event",
                         "why": "Redfish event subscriptions (only if the receiver is enabled)"})
    bbmd = pool.get("bbmd_settings") or {}
    if bbmd.get("enabled") and bbmd.get("bbmd"):
        host, _, port = str(bbmd["bbmd"]).rpartition(":")
        rows.append({"direction": "outbound", "from": collectors, "to": host,
                     "transport": "udp", "port": int(port) if port.isdigit() else 47808,
                     "protocol": "bacnet_fdr",
                     "why": "BACnet Foreign Device Registration with the pool's BBMD"})
    return {"pool_id": pool["id"], "pool": pool["name"], "site": pool.get("site"),
            "plane": pool["plane"], "rows": rows, "text": render_matrix(pool, rows)}


def render_matrix(pool: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    """The same rows as a plain-text change request: one line each,
    nothing a ticketing system can mangle."""
    lines = [f"Firewall matrix - pool {pool['name']} ({pool.get('site')}/{pool['plane']})", ""]
    for r in rows:
        lines.append(f"{r['direction']:<8} {r['from']} -> {r['to']}  "
                     f"{r['transport']}/{r['port']}  [{r['protocol']}]  {r['why']}")
    return "\n".join(lines) + "\n"
