#!/usr/bin/env python3
"""Keep collector/internal/mapping/embedded/ a byte-for-byte copy of
contracts/mappings/.

go:embed cannot reach outside the collector module - contracts/mappings lives
at the monorepo root, one level above collector/ - so a live copy is
committed inside the module instead of a symlink, which this project's mixed
WSL/Windows checkouts cannot be relied on to preserve. This script is that
copy's source of truth, the same role contracts/codegen.py plays for
generated contract code: run it after editing a mapping, and CI's --check
fails the build if anyone forgot to.

    python scripts/sync_mapping_bundle.py            # write the copy
    python scripts/sync_mapping_bundle.py --check     # CI: fail if stale
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "contracts" / "mappings"
DEST = ROOT / "collector" / "internal" / "mapping" / "embedded"


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def sync() -> None:
    if DEST.exists():
        shutil.rmtree(DEST)
    shutil.copytree(SOURCE, DEST)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="fail if the embedded copy is out of date, "
                             "without writing anything")
    args = parser.parse_args()

    if not SOURCE.is_dir():
        sys.exit(f"no mapping source at {SOURCE}")

    if args.check:
        before = snapshot(DEST) if DEST.is_dir() else {}
        after = snapshot(SOURCE)
        if before == after:
            print("collector/internal/mapping/embedded is up to date")
            return 0
        missing = sorted(set(after) - set(before))
        stale = sorted(set(before) - set(after))
        changed = sorted(k for k in (set(before) & set(after))
                         if before[k] != after[k])
        for k in missing:
            print(f"  missing:  {k}")
        for k in stale:
            print(f"  stale:    {k}")
        for k in changed:
            print(f"  changed:  {k}")
        sys.exit("collector/internal/mapping/embedded is out of date - run "
                 "python scripts/sync_mapping_bundle.py")

    sync()
    print(f"synced {SOURCE} -> {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
