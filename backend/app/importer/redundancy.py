"""Derive the A/B side of every power connection from the graph itself.

Why not from names alone: the naming convention is authoritative only where the
site chose to encode it. On this fleet that is the distribution gear - PDUA,
RPPB, UPSA, MPPB - and nothing above it. The upstream plant is numbered
(SWGR1/SWGR2, ATS1/ATS2, GEN1/GEN2), and reading those numbers as sides is
worse than leaving them blank, because it is wrong in the specific way that
matters:

    UTIL1 -> SWGR1 -+-> ATS1 -+-> UPSA -> RPPA* -> PDUA* -> IT load
                    |         +-> MCC1 -> MPPA* -> mechanical load
    GEN1,GEN2 -> SWGR2 -+
                    +-> ATS2 -+-> UPSB -> RPPB* -> PDUB* -> IT load
                              +-> MCC2 -> MPPB* -> mechanical load

SWGR1 and SWGR2 each feed BOTH transfer switches: they are the normal and
emergency sources, not the A and B sides. Labelling SWGR1 as 'A' would tell the
redundancy check that losing it costs you one path, when losing utility power
actually hits A and B at once - which is the entire reason the generators
exist. A shared element MUST come back with no side, because "no side" is how
the correlation logic recognises a common-mode failure.

So: seed from the names that genuinely carry the convention, then propagate
through the topology, and let anything that reaches both paths stay blank.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

log = get_logger("importer.redundancy")

# Distribution gear whose leading token ends in A or B: PDUA-DC1-HA-R1-01,
# RPPB-DC1-CP, UPSA-DC1-UR, MPPB-DC1-HA. Anchored at the start because the
# letter is only meaningful in the role token - a rack called R1-A must not
# make its contents side A.
_SEED_NAME = re.compile(r"^(?:PDU|RPP|MPP|UPS)([AB])(?:[-_]|$)", re.IGNORECASE)

# Sentinel for a device that provably sits on both paths. Distinct from None,
# which means "no evidence either way" - the difference matters, because BOTH
# stops propagation from guessing while None lets a later pass fill it in.
BOTH = "BOTH"


def seed_side(device_name: str) -> str | None:
    m = _SEED_NAME.match(device_name or "")
    return m.group(1).upper() if m else None


def _resolve(sides: dict[str, str], candidates: list[str]) -> str | None:
    """One side if every piece of evidence agrees, BOTH if it conflicts."""
    known = {sides[c] for c in candidates if sides.get(c) in ("A", "B")}
    if not known:
        return None
    return known.pop() if len(known) == 1 else BOTH


def derive_sides(devices: dict[str, str],
                 edges: list[tuple[str, str]]) -> dict[str, str]:
    """Map device id -> 'A' | 'B' | BOTH.

    ``edges`` are (feeder, fed) device-id pairs; direction is meaningful on the
    power layer and this whole derivation depends on it.

    Each pass recomputes every derived side from the PREVIOUS pass's values
    rather than from values other devices picked up earlier in the same loop.
    Mutating in place made the answer depend on dictionary order: SWGR1 could
    see ATS1 resolve to A before ATS2 resolved to B, conclude "A", and - since
    a settled A or B was never revisited - stay wrong forever, quietly turning
    the shared utility feed into a single-path element. That is the exact
    mislabelling this module exists to avoid, so the iteration has to be
    order-independent.

    BOTH is absorbing. Evidence that a device sits on two paths is a
    conclusion; later evidence can never narrow it back to one side.
    """
    seeds = {dev_id: side for dev_id, name in devices.items()
             if (side := seed_side(name))}

    feeders_of: dict[str, list[str]] = defaultdict(list)
    fed_by: dict[str, list[str]] = defaultdict(list)
    for a, b in edges:
        feeders_of[b].append(a)
        fed_by[a].append(b)

    sides: dict[str, str] = dict(seeds)
    for _ in range(len(devices) + 1):
        nxt = dict(seeds)
        for dev_id in devices:
            if dev_id in seeds:
                continue                    # the name is authoritative
            previous = sides.get(dev_id)
            if previous == BOTH:
                nxt[dev_id] = BOTH          # absorbing
                continue
            # Downstream first: what feeds me. A load cabled to PDUA and PDUB
            # is on both paths, which is what a healthy dual-fed load is.
            got = _resolve(sides, feeders_of.get(dev_id, []))
            # Then upstream: what I feed. This is what makes ATS1 side A
            # without its name saying so, and what keeps SWGR1 blank.
            if got is None:
                got = _resolve(sides, fed_by.get(dev_id, []))
            if got is not None:
                nxt[dev_id] = got
        if nxt == sides:
            break
        sides = nxt
    return sides


def edge_side(a_side: str | None, b_side: str | None) -> str | None:
    """The side of one conductor.

    The feeder decides when it has a side: a cord leaving PDUA is on path A
    even though the dual-fed server it lands on is on both.

    When the feeder has no side the conductor takes the side of what it feeds,
    which is what makes the SWGR1 -> ATS1 link part of path A. Both blank, or
    either side BOTH with nothing better, leaves the conductor unlabelled -
    the shared trunk genuinely belongs to no single path.
    """
    if a_side in ("A", "B"):
        return a_side
    if b_side in ("A", "B"):
        return b_side
    return None


async def recompute_power_sides(session: AsyncSession) -> dict[str, Any]:
    """Recompute redundancy_side for every power connection.

    Derived data, so it is safe to run at any time and is rerun after every
    import rather than migrated once.

    Only the power layer. Cooling redundancy on this fleet is N+1 pumps and
    staged chillers rather than two labelled paths, so the same A/B derivation
    would be inventing a structure the plant does not have.
    """
    rows = (await session.execute(text("""
        SELECT DISTINCT c.a_device_id::text AS a, c.b_device_id::text AS b,
               da.name AS a_name, db.name AS b_name
          FROM connection c
          JOIN device da ON da.id = c.a_device_id
          JOIN device db ON db.id = c.b_device_id
         WHERE c.layer = CAST('power' AS layer_t)
    """))).mappings().all()

    devices: dict[str, str] = {}
    edges: list[tuple[str, str]] = []
    for r in rows:
        devices[r["a"]] = r["a_name"]
        devices[r["b"]] = r["b_name"]
        edges.append((r["a"], r["b"]))

    sides = derive_sides(devices, edges)

    # One UPDATE driven by a VALUES list rather than a statement per edge.
    pairs = {(a, b) for a, b in edges}
    payload = []
    for a, b in pairs:
        side = edge_side(sides.get(a), sides.get(b))
        if side:
            payload.append({"a": a, "b": b, "side": side})

    await session.execute(text("""
        UPDATE connection SET redundancy_side = NULL
         WHERE layer = CAST('power' AS layer_t)
    """))
    if payload:
        await session.execute(text("""
            UPDATE connection c SET redundancy_side = v.side
              FROM (SELECT CAST(:a AS uuid) AS a, CAST(:b AS uuid) AS b,
                           CAST(:side AS char(1)) AS side) v
             WHERE c.layer = CAST('power' AS layer_t)
               AND c.a_device_id = v.a AND c.b_device_id = v.b
        """), payload)

    counts = {"A": 0, "B": 0, "shared_or_unknown": 0}
    for dev_id in devices:
        side = sides.get(dev_id)
        counts[side if side in ("A", "B") else "shared_or_unknown"] += 1

    result = {"device_pairs": len(pairs), "sided_pairs": len(payload),
              "devices": len(devices), **counts}
    log.info("power redundancy sides recomputed", **result)
    return result
