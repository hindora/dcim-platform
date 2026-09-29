"""Fingerprint for the platform's own copy of the mapping bundle.

Reimplements contracts/mapping_bundle.py rather than importing it: that
directory is a monorepo-root tool directory (schema, mapping YAML, codegen
scripts), not part of the installed backend package, and is not on sys.path
in either the dev checkout or the Docker image. The two are kept from
drifting apart by tests/test_mapping_bundle.py, which imports both by path
against the same fixtures and asserts they agree.

A third implementation - collector/internal/mapping/bundle.go - is what this
exists to check against: same file set, same sort order, same separator
bytes, so a collector's reported digest and this one mean the same thing.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

# This file sits at backend/app/services/mapping_bundle.py, so parents[3] is
# the directory backend/ itself sits in: the repo root in a dev checkout, and
# /app in the Docker image (backend/Dockerfile does `COPY backend /app/backend`
# and `COPY contracts /app/contracts` as siblings under /app). Both give the
# same relative shape, <root>/contracts/mappings.
MAPPINGS_DIR = Path(__file__).resolve().parents[3] / "contracts" / "mappings"


def bundle_sha(root: Path) -> str:
    files = sorted(p for p in root.rglob("*") if p.is_file())
    h = hashlib.sha256()
    for p in files:
        rel = p.relative_to(root).as_posix()
        h.update(rel.encode())
        h.update(b"\x00")
        h.update(p.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()


@lru_cache(maxsize=1)
def expected_sha() -> str | None:
    """The platform's own digest, or None if contracts/mappings is missing.

    Cached: contracts/mappings ships with the release and cannot change
    within a running process, the same assumption the generated contract
    code already makes.
    """
    if not MAPPINGS_DIR.is_dir():
        return None
    return bundle_sha(MAPPINGS_DIR)
