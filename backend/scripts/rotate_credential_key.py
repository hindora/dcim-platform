#!/usr/bin/env python3
"""Re-wrap every device credential under a new key, in place (docs/26 Phase 4).

Before running this against a real key rotation, add the OLD key to
DCIM_CREDENTIAL_KEY_RING under some id (so rows not yet migrated this run
still decrypt) and set DCIM_CREDENTIAL_KEY to the NEW key's value - or, to
rotate INTO the ring instead of onto DCIM_CREDENTIAL_KEY, add the new key to
the ring and pass --target-key-id for it.

    cd backend && ../.venv/bin/python scripts/rotate_credential_key.py \\
        --target-key-id 2026-01

    # or, rotating back onto DCIM_CREDENTIAL_KEY itself:
    ../.venv/bin/python scripts/rotate_credential_key.py --target-key-id ""

Safe to interrupt and re-run: each row is decrypted under whatever key its
OWN key_id currently names, re-encrypted under the target, and both
secret_enc and key_id are written back in the same UPDATE - so a row is
never left with one column updated and not the other. A row already at the
target key_id is skipped, which is what makes re-running after a partial
run - or after adding a row's actual key back into the ring because the
first attempt failed - pick up exactly where it left off rather than
redoing (or corrupting) work that already succeeded.

A row that fails to decrypt (its key_id names an entry no longer in the
ring, most likely) is reported and left untouched, never dropped: the
platform's own decrypt_failures handling at serve time already treats an
undecryptable credential as an outage for that one endpoint, not data loss,
and this script keeps the same posture rather than silently discarding rows
it cannot currently read.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import decrypt_secret, encrypt_secret
from app.db.session import unit_of_work


@dataclass
class RotationResult:
    migrated: int = 0
    skipped_already_current: int = 0
    failed_ids: list[str] = field(default_factory=list)


async def rotate(session: AsyncSession, settings: Settings, target_key_id: str | None,
                 batch_size: int = 200, commit_every_batch: bool = True) -> RotationResult:
    """The re-wrap loop itself, taking an existing session rather than
    opening one - what a live test runs against a transaction it rolls back
    afterward, and what main() below runs against a real committed one.

    commit_every_batch is what makes a real run resumable across a crash;
    a caller managing its own transaction (a test) passes False so nothing
    here commits underneath it.
    """
    result = RotationResult()
    rows = (await session.execute(text(
        "SELECT id::text, secret_enc, key_id FROM credential"))).mappings().all()

    pending = [r for r in rows if r["key_id"] != target_key_id]
    result.skipped_already_current = len(rows) - len(pending)

    for i in range(0, len(pending), batch_size):
        batch = pending[i:i + batch_size]
        for row in batch:
            try:
                plain = decrypt_secret(bytes(row["secret_enc"]),
                                      key_id=row["key_id"], settings=settings)
            except Exception as exc:
                result.failed_ids.append(row["id"])
                print(f"  SKIP {row['id']}: could not decrypt under "
                      f"key_id={row['key_id']!r}: {exc}", file=sys.stderr)
                continue
            new_blob = encrypt_secret(plain, key_id=target_key_id, settings=settings)
            await session.execute(text("""
                UPDATE credential SET secret_enc = :blob, key_id = :key_id
                WHERE id = :id
            """), {"blob": new_blob, "key_id": target_key_id, "id": row["id"]})
            result.migrated += 1
        if commit_every_batch:
            await session.commit()
        print(f"  {result.migrated}/{len(pending)} re-wrapped...")

    return result


async def main(target_key_id: str | None, batch_size: int) -> int:
    settings = get_settings()
    # Fails fast and loudly if target_key_id names a ring entry that does
    # not exist, rather than discovering that after decrypting the first
    # batch and being unable to write any of it back correctly.
    try:
        settings.credential_key_for(target_key_id)
    except ValueError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 1

    async with unit_of_work() as session:
        result = await rotate(session, settings, target_key_id, batch_size)

    print(f"done: {result.migrated} re-wrapped, {result.skipped_already_current} "
         f"already at key_id={target_key_id!r}, {len(result.failed_ids)} failed "
         f"(left untouched: {result.failed_ids})")
    return 1 if result.failed_ids else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target-key-id", required=True,
                        help='the key_id to rotate every row onto; "" means '
                             "DCIM_CREDENTIAL_KEY itself, not a ring entry")
    parser.add_argument("--batch-size", type=int, default=200,
                        help="rows committed per transaction (default 200)")
    args = parser.parse_args()
    target = args.target_key_id or None
    raise SystemExit(asyncio.run(main(target, args.batch_size)))
