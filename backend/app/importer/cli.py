"""Seed the DCIM inventory from a running simulator.

    python -m app.importer.cli --base-url http://127.0.0.1:8001 \
                               --username admin --password admin1234

Idempotent: re-run it after any fleet change. Use --protocols to widen beyond
SNMP as the other adapters land.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine, unit_of_work
from app.importer.simulator import TopologyImporter, fetch_topology

log = get_logger("importer.cli")


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()

    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            topology = json.load(fh)
        log.info("loaded topology from file", path=args.file)
    else:
        password = args.password or (
            settings.simulator_password.get_secret_value()
            if settings.simulator_password else None)
        if not password:
            print("no password: pass --password or set DCIM_SIMULATOR_PASSWORD",
                  file=sys.stderr)
            return 2
        topology = await fetch_topology(args.base_url, args.username, password)
        log.info("fetched topology", base_url=args.base_url,
                 nodes=len(topology.get("nodes") or []),
                 edges=len(topology.get("edges") or []))

    protocols = frozenset(p.strip() for p in args.protocols.split(",") if p.strip())

    async with unit_of_work() as session:
        importer = TopologyImporter(
            session,
            include_protocols=protocols,
            gnmi_server_host=args.gnmi_host or settings.gnmi_server_host,
            collector_id=args.collector_id,
        )
        report = await importer.run(topology)

    print(json.dumps(report.as_dict(), indent=2, default=str))
    if report.warnings:
        print(f"\n{len(report.warnings)} warning(s):", file=sys.stderr)
        for w in report.warnings[:20]:
            print(f"  {w}", file=sys.stderr)
    await dispose_engine()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--base-url", default=None,
                     help="simulator API base URL (default: DCIM_SIMULATOR_BASE_URL)")
    src.add_argument("--file", help="import a topology export from a JSON file instead")
    ap.add_argument("--username", default=None)
    ap.add_argument("--password", default=None,
                    help="prefer DCIM_SIMULATOR_PASSWORD over passing this on a command line")
    ap.add_argument("--protocols", default="snmp",
                    help="comma-separated protocols to create endpoints for "
                         "(phase 1 ships the SNMP adapter only)")
    ap.add_argument("--gnmi-host", default=None)
    ap.add_argument("--collector-id", default=None,
                    help="assign the created endpoints to this collector shard")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    settings = get_settings()
    args.base_url = args.base_url or settings.simulator_base_url
    args.username = args.username or settings.simulator_username

    configure_logging(level=args.log_level, service="dcim-importer")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
