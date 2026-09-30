"""The shard map and collector drains (docs/26 Phase 5's frontend row).

Two owners are shown for every endpoint:

- **served**: who `build_assignment` hands it to - `sharding.effective`:
  a pin, else the assigner's record while it is valid, else the live plan.
  This is who is polling it.
- **recorded**: what the assigner last wrote to `endpoint_assignment`, with
  epoch, since and reason, and the history every move leaves (migration
  0093).

Since the serving path reads the record, they agree except in the window
between a change and the assigner's next tick (an owner drained, retired
or moved out of the pool), or where a pin is newer than the record. A
persistent disagreement means the assigner is not running.

Drain follows Kubernetes' `drain`, the most widely understood version of the
operation: preview what moves and where, refuse when something would have
nowhere to go unless forced, leave pinned work alone unless the operator
releases it, and make the move now rather than on the next tick.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import collector as repo
from app.repositories import pools as pools_repo
from app.services import sharding
from app.services.collector import fleet, ownership

# ---------------------------------------------------------------- shard map

def build_rows(display: list[dict[str, Any]], ownable: list[dict[str, Any]],
               served: dict[str, str | None],
               pool_names: dict[str, str]) -> list[dict[str, Any]]:
    """Pure: one row per endpoint, served and recorded owners side by side."""
    placement = {str(e["id"]): e for e in ownable}
    out = []
    for d in display:
        eid = d["id"]
        p = placement.get(eid, {})
        owner = served.get(eid)
        recorded = d.get("recorded_owner")
        has_record = d.get("reason") is not None
        out.append({
            **{k: d.get(k) for k in ("id", "device_id", "device_name", "device_type",
                                     "protocol", "address", "epoch", "since", "reason")},
            "site": p.get("site"),
            "pool_id": p.get("pool_id"),
            "pool_name": pool_names.get(p.get("pool_id") or ""),
            "owner": owner,
            "pinned": bool(d.get("pinned_to")),
            "recorded_owner": recorded,
            # Only a real record can disagree: no row yet is "not recorded".
            "record_disagrees": has_record and recorded != owner,
        })
    out.sort(key=lambda r: (r["device_name"] or "", r["protocol"] or "", r["id"]))
    return out


def summarise(rows: list[dict[str, Any]],
              collectors: list[sharding.Collector]) -> dict[str, Any]:
    """Pure: per-collector counts, and what is owned by nobody."""
    served = Counter(r["owner"] for r in rows if r["owner"])
    pinned = Counter(r["owner"] for r in rows if r["owner"] and r["pinned"])
    known = {c.collector_id for c in collectors}
    by_collector = [
        {"collector_id": c.collector_id, "pool_id": c.pool_id,
         "accepting": c.accepting, "healthy": c.healthy,
         "owned": served.get(c.collector_id, 0),
         "pinned": pinned.get(c.collector_id, 0)}
        for c in sorted(collectors, key=lambda c: c.collector_id)]
    # A pin to a collector not in the fleet (not started yet, or retired) is
    # kept by the plan on purpose - and polled by nobody meanwhile.
    for cid in sorted(set(served) - known):
        by_collector.append({"collector_id": cid, "pool_id": None, "accepting": False,
                             "healthy": False, "owned": served[cid],
                             "pinned": pinned.get(cid, 0), "not_in_fleet": True})
    return {
        "by_collector": by_collector,
        "total": len(rows),
        "unassigned": sum(1 for r in rows if not r["owner"]),
        "record_disagrees": sum(1 for r in rows if r["record_disagrees"]),
        "unrecorded": sum(1 for r in rows if r["reason"] is None),
    }


def filter_rows(rows: list[dict[str, Any]], *, collector_id: str | None = None,
                pool_id: str | None = None, protocol: str | None = None,
                q: str | None = None, disagree_only: bool = False,
                unassigned_only: bool = False) -> list[dict[str, Any]]:
    needle = (q or "").strip().lower()
    out = []
    for r in rows:
        if collector_id and r["owner"] != collector_id:
            continue
        if pool_id and r["pool_id"] != pool_id:
            continue
        if protocol and r["protocol"] != protocol:
            continue
        if disagree_only and not r["record_disagrees"]:
            continue
        if unassigned_only and r["owner"]:
            continue
        if needle and needle not in (r["device_name"] or "").lower() \
                and needle not in (r["address"] or ""):
            continue
        out.append(r)
    return out


async def shard_map(session: AsyncSession, *, limit: int = 50, offset: int = 0,
                    **filters: Any) -> dict[str, Any]:
    served = await ownership(session)
    collectors = await fleet(session)
    ownable = await repo.ownable_endpoints(session)
    pool_names = {p["id"]: p["name"] for p in await pools_repo.list_pools(session)}
    rows = build_rows(await repo.shard_rows(session), ownable, served, pool_names)
    summary = summarise(rows, collectors)
    summary["moves_24h"] = await repo.recent_moves(session, 24)
    summary["pools"] = [{"id": k, "name": v} for k, v in sorted(pool_names.items(),
                                                                key=lambda kv: kv[1])]
    summary["protocols"] = sorted({r["protocol"] for r in rows if r["protocol"]})
    matched = filter_rows(rows, **filters)
    return {"summary": summary, "total": len(matched),
            "items": matched[offset:offset + limit]}


# -------------------------------------------------------------------- drain

def preview_drain(collector_id: str, ownable: list[dict[str, Any]],
                  collectors: list[sharding.Collector],
                  recorded: dict[str, str | None] | None = None) -> dict[str, Any]:
    """Pure: what draining this collector would do, before doing it.

    "Owned now" is `sharding.effective` - what the serving path hands it.
    Where each of those goes is the fresh plan with this collector no
    longer accepting: a drain is a mandatory move, and the assigner moves
    every mandatory endpoint straight to its target, undamped - so the
    preview is exactly the move, not an estimate of it."""
    now = (sharding.effective(ownable, collectors, recorded or {})
           if collectors else {})
    drained = [replace(c, accepting=False) if c.collector_id == collector_id else c
               for c in collectors]
    after = sharding.plan(ownable, drained) if drained else {}
    by_id = {str(e["id"]): e for e in ownable}

    owned = [eid for eid, owner in now.items() if owner == collector_id]
    destinations: Counter[str] = Counter()
    stranded_pools: Counter[str] = Counter()
    pinned = moving = stranded = 0
    for eid in owned:
        dest = after.get(eid)
        if dest == collector_id:
            pinned += 1
        elif dest is None:
            stranded += 1
            stranded_pools[by_id.get(eid, {}).get("pool_id") or ""] += 1
        else:
            moving += 1
            destinations[dest] += 1

    blockers = []
    if stranded:
        blockers.append(
            f"{stranded} endpoint(s) would have nowhere to go: no other collector "
            f"that serves their pool or site is accepting work. They would be "
            f"polled by nothing until one is.")
    return {
        "collector_id": collector_id,
        "owned": len(owned),
        "moving": moving,
        "destinations": dict(sorted(destinations.items())),
        "stranded": stranded,
        "stranded_pools": {k or None: v for k, v in stranded_pools.items()},
        # Pins beat the hash (sharding.plan) - a drain leaves them where they
        # are, and the collector cannot be emptied until they are released.
        "pinned": pinned,
        "blockers": blockers,
        "can_drain": not blockers,
    }


async def drain_preview(session: AsyncSession, collector_id: str) -> dict[str, Any]:
    return preview_drain(collector_id, await repo.ownable_endpoints(session),
                         await fleet(session), await repo.current_assignment(session))


async def remaining(session: AsyncSession, collector_id: str) -> int:
    """What a draining collector is still served - its pins, and anything
    the plan still gives it."""
    return sum(1 for owner in (await ownership(session)).values()
               if owner == collector_id)
