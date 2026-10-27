"""Re-seat the platform's power cords after the simulator re-cabled its strips.

    python -m app.importer.resync_outlets --file topology.json            # dry run
    python -m app.importer.resync_outlets --file topology.json --apply

The simulator fixture re-cabled every rack strip (2026-10-27): cords spread over
legs and banks instead of filling outlets in order, and each bank now carries
both connector types. The full importer cannot follow that. A connection's
identity includes its outlet, so re-importing a cord that moved from outlet 3 to
outlet 9 INSERTS a second cord beside the first instead of moving it - and the
outlet actions on the device page would then switch the wrong socket.

This does exactly two things, in one transaction:

  1. outlet rows: connector, phase and branch (the bank) per (strip, number),
     from the export - the receptacle at outlet 9 may now be a C19 on bank 2;
  2. power connections: each cord's strip-side termination moved to the outlet
     the export now names, found by (strip, load, PSU). Moving cords are first
     parked on no termination and then set, because one cable per outlet is
     enforced (uq_connection_a_termination) and two cords swapping outlets
     would otherwise collide half way.

Nothing else is touched: no device, no endpoint, no other layer. Idempotent - a
second run moves nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from sqlalchemy import text

from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine, unit_of_work

log = get_logger("importer.resync_outlets")


def plan_moves(edges: list[dict[str, Any]], device_id: dict[str, str],
               outlet_id: dict[tuple[str, int], str], psu_id: dict[tuple[str, int], str],
               current: dict[tuple[str, str, str], tuple[str, str | None]]
               ) -> tuple[list[tuple[str, str]], list[str]]:
    """[(connection_id, new outlet id)] for every cord whose outlet changed, and
    what could not be matched.

    `current` maps (strip id, load id, psu row id) to (connection id, its
    present outlet row id).
    """
    moves: list[tuple[str, str]] = []
    unmatched: list[str] = []
    for e in edges:
        if e.get("layer") != "power" or e.get("outlet") is None or e.get("psu") is None:
            continue
        strip = device_id.get(e.get("supply_node") or e.get("src") or "")
        load = device_id.get(e.get("load_node") or e.get("dst") or "")
        if not strip or not load:
            unmatched.append(f"{e.get('src')}->{e.get('dst')}: device not in the platform")
            continue
        want = outlet_id.get((strip, int(e["outlet"])))
        psu = psu_id.get((load, int(e["psu"])))
        if not want or not psu:
            unmatched.append(f"{e.get('src')}->{e.get('dst')}: outlet {e['outlet']} or "
                             f"PSU {e['psu']} not in the platform")
            continue
        row = current.get((strip, load, psu))
        if row is None:
            unmatched.append(f"{e.get('src')}->{e.get('dst')}: no cord in the platform")
            continue
        conn_id, have = row
        if have != want:
            moves.append((conn_id, want))
    return moves, unmatched


async def _resync(topology: dict[str, Any], apply: bool) -> int:
    nodes = topology.get("nodes") or []
    edges = topology.get("edges") or []
    async with unit_of_work() as s:
        dev_rows = (await s.execute(text(
            "SELECT external_id, id::text FROM device WHERE external_id IS NOT NULL"
        ))).all()
        device_id = dict(dev_rows)

        # 1. outlet rows
        outlets_changed = 0
        for n in nodes:
            d = n.get("device") or {}
            did = device_id.get(d.get("id") or n.get("id") or "")
            if not did or d.get("device_type") != "pdu":
                continue
            for o in d.get("outlets") or []:
                num = o.get("number", o.get("index"))
                if num is None:
                    continue
                res = await s.execute(text("""
                    UPDATE outlet SET connector = :conn, phase = :phase, branch = :branch
                     WHERE device_id = CAST(:dev AS uuid) AND number = :num
                       AND (connector IS DISTINCT FROM :conn OR phase IS DISTINCT FROM :phase
                            OR branch IS DISTINCT FROM :branch)
                """), {"dev": did, "num": int(num),
                       "conn": o.get("connector") or o.get("type") or "C13",
                       "phase": o.get("phase"),
                       "branch": str(o["bank"]) if o.get("bank") is not None else o.get("branch")})
                outlets_changed += res.rowcount or 0

        outlet_id = {(dev, int(num)): oid for dev, num, oid in (await s.execute(text(
            "SELECT device_id::text, number, id::text FROM outlet"))).all()}
        psu_id = {(dev, int(num)): pid for dev, num, pid in (await s.execute(text(
            "SELECT device_id::text, number, id::text FROM power_supply"))).all()}
        current = {(a, b, psu): (cid, aterm) for cid, a, b, psu, aterm in (await s.execute(text("""
            SELECT id::text, a_device_id::text, b_device_id::text,
                   b_termination_id::text, a_termination_id::text
              FROM connection
             WHERE layer = CAST('power' AS layer_t)
               AND b_termination_type = CAST('psu' AS termination_t)
        """))).all()}

        moves, unmatched = plan_moves(edges, device_id, outlet_id, psu_id, current)
        print(f"outlet rows {'updated' if apply else 'to update'}: {outlets_changed}")
        print(f"cords {'moved' if apply else 'to move'}: {len(moves)}")
        print(f"unmatched: {len(unmatched)}")
        for u in unmatched[:10]:
            print(f"  {u}")

        if not apply:
            await s.rollback()
            print("dry run: nothing written (pass --apply)")
            return 0

        # 2. park, then set: one cable per outlet is enforced.
        ids = [m[0] for m in moves]
        if ids:
            await s.execute(text("""
                UPDATE connection SET a_termination_type = CAST('none' AS termination_t),
                                      a_termination_id = NULL
                 WHERE id = ANY(CAST(:ids AS uuid[]))
            """), {"ids": ids})
            for conn_id, oid in moves:
                await s.execute(text("""
                    UPDATE connection SET a_termination_type = CAST('outlet' AS termination_t),
                                          a_termination_id = CAST(:oid AS uuid)
                     WHERE id = CAST(:cid AS uuid)
                """), {"oid": oid, "cid": conn_id})
        log.info("power outlets resynced", outlets=outlets_changed, cords=len(moves),
                 unmatched=len(unmatched))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--file", required=True, help="the simulator's topology export (JSON)")
    ap.add_argument("--apply", action="store_true", help="write; without it, a dry run")
    args = ap.parse_args()
    configure_logging("INFO")
    with open(args.file, encoding="utf-8-sig") as fh:
        topology = json.load(fh)

    async def run() -> int:
        try:
            return await _resync(topology, args.apply)
        finally:
            await dispose_engine()
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
