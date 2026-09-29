#!/usr/bin/env python3
"""One-time bootstrap for the collector enrollment CA (docs/26 Phase 2).

Generates a root and an intermediate, stores the intermediate (cert AND key,
encrypted with DCIM_CREDENTIAL_KEY) in the database so the running platform
can sign enrollments, and writes the root's cert AND key to files - the only
copy this tool ever makes of the root key, because the running platform
never loads it again after this script exits.

    cd backend && ../.venv/bin/python scripts/init_ca.py --out ./ca-root

Move ./ca-root/root.key.pem offline (a password manager, an HSM, a safe -
anywhere this platform's own process cannot reach) before anyone forgets it
exists. ./ca-root/root.cert.pem is not a secret; it is what every collector
and the TLS proxy are configured to trust, and it is safe to publish or
commit to a deployment's own infrastructure repo.

Refuses to run twice: an active intermediate already existing means this
platform already has a CA, and generating a second root here would produce
one nothing trusts and leave the operator holding two "the" root certs.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.db.session import unit_of_work
from app.services import ca, collector_pki


async def main(out_dir: Path) -> int:
    async with unit_of_work() as session:
        existing = (await session.execute(text(
            "SELECT 1 FROM certificate_authority WHERE kind = 'intermediate' "
            "AND is_active"))).first()
        if existing:
            print("an active intermediate CA already exists - refusing to "
                  "generate a second root", file=sys.stderr)
            return 1

        root = ca.generate_root()
        intermediate = ca.generate_intermediate(root.cert_pem, root.key_pem)

        await collector_pki.store_root(session, root.cert_pem, root.serial,
                                       root.not_after)
        await collector_pki.store_intermediate(
            session, intermediate.cert_pem, intermediate.key_pem,
            intermediate.serial, intermediate.not_after, activate=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    root_cert_path = out_dir / "root.cert.pem"
    root_key_path = out_dir / "root.key.pem"
    root_cert_path.write_text(root.cert_pem)
    root_key_path.write_text(root.key_pem)
    with contextlib.suppress(NotImplementedError):  # Windows has no chmod bits
        root_key_path.chmod(0o600)

    print(f"root:         {root_cert_path}  (serial {root.serial}, "
          f"expires {root.not_after.date()})")
    print(f"root key:     {root_key_path}  <- move this OFFLINE now")
    print(f"intermediate: stored in the database (serial {intermediate.serial}, "
          f"expires {intermediate.not_after.date()}), active")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("./ca-root"),
                        help="directory to write the root cert and key to")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.out)))
