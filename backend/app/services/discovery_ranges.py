"""Discovery ranges, and turning a choice of them into runs a collector can take.

A range is address space somebody said is worth auditing (migration 0082). This
module validates them, and queues sweeps of them: one run per collector that has
to do the work, each no wider than a collector will sweep.

`DiscoveryError` lives here so services.discovery can re-export it without an
import cycle - both modules raise it, and the API catches one class.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.repositories import discovery as run_repo
from app.repositories import discovery_ranges as repo

log = get_logger(__name__)


class DiscoveryError(ValueError):
    """Bad request, with a message meant for the caller."""


#: What a range is FOR. The plane decides what a sweep there should find - a BMS
#: range full of servers is itself a finding - and which collector sits on it.
PURPOSES = ("it_oob", "bms", "production", "other")

#: The collector's own ceiling (discovery.MaxAddresses). A run wider than this is
#: refused on the collector rather than truncated, so the API never builds one.
MAX_ADDRESSES = 4096

#: A /20 is 4094 hosts - the widest range that fits in one sweep.
WIDEST_PREFIX = 20

SWEEP_METHODS = frozenset({"sweep", "snmp_sweep"})


# ----------------------------------------------------------------- validation

def parse_cidr(raw: Any) -> ipaddress.IPv4Network:
    """An IPv4 network a sweep can cover, or a DiscoveryError saying why not."""
    s = str(raw or "").strip()
    if not s:
        raise DiscoveryError("a range needs a CIDR, e.g. 10.51.11.0/24")
    if "/" not in s:
        raise DiscoveryError(f"{s!r} has no prefix length; a single address is "
                             f"{s}/32")
    try:
        net = ipaddress.ip_network(s, strict=True)
    except ValueError as exc:
        # The commonest typo, answered with the fix rather than the parser's words.
        if "host bits set" in str(exc):
            loose = ipaddress.ip_network(s, strict=False)
            raise DiscoveryError(f"{s} has host bits set; the range is {loose}") \
                from None
        raise DiscoveryError(f"{s!r} is not a CIDR, e.g. 10.51.11.0/24") from None
    if net.version != 4:
        raise DiscoveryError("only IPv4 ranges can be swept")
    if net.prefixlen < WIDEST_PREFIX:
        raise DiscoveryError(
            f"{net} is {net.num_addresses:,} addresses; one sweep covers at most "
            f"{MAX_ADDRESSES} (a /{WIDEST_PREFIX}). Split it into smaller ranges")
    return net


def parse_exclusions(raw: Any, net: ipaddress.IPv4Network) -> list[str]:
    """Addresses inside `net` a sweep must not probe. A bare address is a /32."""
    items = raw if isinstance(raw, list) else str(raw or "").replace(",", " ").split()
    out: list[ipaddress.IPv4Network] = []
    for item in items:
        s = str(item).strip()
        if not s:
            continue
        try:
            ex = ipaddress.ip_network(s if "/" in s else f"{s}/32", strict=False)
        except ValueError:
            raise DiscoveryError(f"exclusion {s!r} is not an address or CIDR") from None
        if ex.version != 4 or not ex.subnet_of(net):
            raise DiscoveryError(f"exclusion {ex} is outside {net}")
        out.append(ex)
    return [str(x) for x in ipaddress.collapse_addresses(out)]


def probe_count(net: ipaddress.IPv4Network, exclusions: list[str]) -> int:
    """How many addresses a sweep of `net` actually sends to: minus network and
    broadcast below /31 (as the collector's Hosts() does), minus exclusions."""
    total = net.num_addresses
    skipped = {net.network_address, net.broadcast_address} if net.prefixlen < 31 else set()
    hosts = total - len(skipped)
    for ex in exclusions:
        e = ipaddress.ip_network(ex)
        hosts -= e.num_addresses - len(
            [a for a in (e.network_address, e.broadcast_address) if a in skipped])
    return max(hosts, 0)


def _clean(payload: dict[str, Any], *, partial: bool,
           current: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    net = None
    if "cidr" in payload or not partial:
        net = parse_cidr(payload.get("cidr"))
        out["cidr"] = str(net)
    elif current:
        net = ipaddress.ip_network(current["cidr"])
    if "exclusions" in payload or "cidr" in out:
        raw = payload.get("exclusions", current.get("exclusions") if current else [])
        out["exclusions"] = parse_exclusions(raw or [], net)  # type: ignore[arg-type]
    if "name" in payload or not partial:
        name = str(payload.get("name") or "").strip()
        out["name"] = name or out.get("cidr") or (current or {}).get("cidr")
    if "purpose" in payload or not partial:
        purpose = payload.get("purpose") or None
        if purpose is not None and purpose not in PURPOSES:
            raise DiscoveryError(f"purpose must be one of {', '.join(PURPOSES)}")
        out["purpose"] = purpose
    for key in ("datacenter_id", "collector_id", "notes"):
        if key in payload or not partial:
            val = payload.get(key)
            out[key] = (str(val).strip() or None) if val is not None else None
    if "enabled" in payload or not partial:
        out["enabled"] = bool(payload.get("enabled", True))
    return out


# ---------------------------------------------------------------------- CRUD

async def create_range(session: AsyncSession, payload: dict[str, Any],
                       actor: str | None) -> dict[str, Any]:
    values = _clean(payload, partial=False)
    await _check(session, values)
    row = await repo.create_range(session, values, actor)
    log.info("discovery range created", cidr=values["cidr"], name=values["name"])
    return row


async def update_range(session: AsyncSession, range_id: str,
                       payload: dict[str, Any]) -> dict[str, Any]:
    current = await repo.get_range(session, range_id)
    if current is None:
        raise DiscoveryError("no such range")
    values = _clean(payload, partial=True, current=current)
    await _check(session, values, except_id=range_id)
    return await repo.update_range(session, range_id, values)  # type: ignore[return-value]


async def delete_range(session: AsyncSession, range_id: str) -> None:
    """Refused while a schedule sweeps it: deleting it would quietly shrink what
    that schedule audits, and the schedule would go on reporting "all clear"."""
    users = await repo.schedules_using(session, range_id)
    if users:
        raise DiscoveryError(
            f"schedule{'s' if len(users) > 1 else ''} {', '.join(users)} "
            f"sweep{'' if len(users) > 1 else 's'} this range; remove it there first")
    if not await repo.delete_range(session, range_id):
        raise DiscoveryError("no such range")


async def _check(session: AsyncSession, values: dict[str, Any],
                 except_id: str | None = None) -> None:
    if "cidr" in values:
        taken = await repo.cidr_taken(session, values["cidr"], except_id)
        if taken:
            raise DiscoveryError(f"{values['cidr']} is already the range {taken!r}")
    if values.get("datacenter_id"):
        known = {d["id"] for d in await repo.datacenters(session)}
        if values["datacenter_id"] not in known:
            raise DiscoveryError("no such site")


# ---------------------------------------------------------------------- runs

def _pack(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group ranges into runs of at most MAX_ADDRESSES probes, in order.

    Split rather than refused: each range fits in one sweep by construction
    (masklen >= 20), so a selection too wide for one run is several runs, each
    of which sweeps its ranges completely. Nothing is truncated."""
    runs: list[list[dict[str, Any]]] = []
    size = 0
    for it in items:
        if not runs or size + it["probes"] > MAX_ADDRESSES:
            runs.append([])
            size = 0
        runs[-1].append(it)
        size += it["probes"]
    return runs


async def queue(session: AsyncSession, *, method: str = "sweep",
                range_ids: list[str] | None = None,
                subnets: list[str] | None = None,
                collector_id: str | None = None,
                schedule_id: str | None = None,
                schedule_label: str | None = None) -> list[dict[str, Any]]:
    """Queue sweeps of saved ranges and/or one-off subnets. Returns the runs.

    One run per collector that has to do the work, because the collector is what
    sits on the network: a range assigned to DC2's collector is swept by DC2's
    collector, never by whichever one polled first. A one-off subnet has no
    record to say which collector reaches it, so it goes to `collector_id`, or to
    any.
    """
    if method not in SWEEP_METHODS:
        raise DiscoveryError(
            f"method {method!r} is not implemented; "
            f"try one of {', '.join(sorted(SWEEP_METHODS))}")
    range_ids = [r for r in (range_ids or []) if r]
    subnets = [s for s in (subnets or []) if s and str(s).strip()]
    if not range_ids and not subnets:
        raise DiscoveryError("choose at least one range or subnet to sweep")
    # Validated before anything touches the database, so a typo fails at request
    # time rather than silently sweeping nothing an hour later on a collector.
    adhoc = []
    for s in subnets:
        net = parse_cidr(s)
        adhoc.append({"cidr": str(net), "exclusions": [], "id": None,
                      "collector_id": (collector_id or None), "probes": probe_count(net, [])})

    chosen = await repo.get_ranges(session, range_ids) if range_ids else []
    missing = set(range_ids) - {r["id"] for r in chosen}
    if missing:
        raise DiscoveryError("a chosen range no longer exists; reload and try again")
    off = [r["name"] for r in chosen if not r["enabled"]]
    if off:
        raise DiscoveryError(f"{', '.join(off)} {'is' if len(off) == 1 else 'are'} "
                             f"disabled; enable it to sweep it")
    items = [{**r, "probes": probe_count(ipaddress.ip_network(r["cidr"]),
                                         list(r["exclusions"] or []))}
             for r in chosen] + adhoc

    groups: dict[str | None, list[dict[str, Any]]] = {}
    for it in items:
        groups.setdefault(it["collector_id"], []).append(it)

    runs = []
    for target, group in groups.items():
        for part in _pack(group):
            scope: dict[str, Any] = {"subnets": [p["cidr"] for p in part]}
            exclude = [e for p in part for e in (p["exclusions"] or [])]
            if exclude:
                scope["exclude"] = exclude
            run = await run_repo.create_run(
                session, method=method, scope=scope, schedule_id=schedule_id,
                schedule_label=schedule_label, collector_id=target,
                range_ids=[p["id"] for p in part if p["id"]])
            runs.append(run)
            log.info("discovery run queued", run_id=run["id"], collector=target,
                     subnets=scope["subnets"], excluded=len(exclude))
    return runs


#: How late a scheduled range may be before it reads as overdue: twice its
#: schedule's longest gap. Once is a slow collector or a run that waited its
#: turn; twice is an audit that has stopped happening.
OVERDUE_FACTOR = 2


def audit_state(r: dict[str, Any], now_s: float) -> str:
    """How fresh this range's audit is.

    ok - fully swept within twice its schedule's gap; overdue - not; never - on
    a schedule and never fully swept; unscheduled - nothing sweeps it on its own;
    off - disabled. A schedule that is paused, failing or skipping the range all
    end up here, which is the point: they are different causes of one outcome.
    """
    if not r.get("enabled"):
        return "off"
    hours = r.get("schedule_hours")
    if hours is None:
        return "unscheduled"
    last = r.get("last_full_at")
    if last is None:
        return "never"
    age = now_s - last.timestamp()
    return "overdue" if age > OVERDUE_FACTOR * float(hours) * 3600 else "ok"


async def list_ranges(session: AsyncSession) -> list[dict[str, Any]]:
    import time as _time
    now_s = _time.time()
    rows = await repo.list_ranges(session)
    for r in rows:
        r["audit"] = audit_state(r, now_s)
    return rows


async def options(session: AsyncSession) -> dict[str, Any]:
    """What the range form offers: sites, collectors, purposes, and the limits."""
    return {"datacenters": await repo.datacenters(session),
            "collectors": await repo.collectors(session),
            "purposes": list(PURPOSES),
            "widest_prefix": WIDEST_PREFIX, "max_addresses": MAX_ADDRESSES}
