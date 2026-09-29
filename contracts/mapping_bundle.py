"""Fingerprint for the mapping bundle collectors embed.

Mirrors collector/internal/mapping/bundle.go byte for byte: same file set
(every regular file under the root), the same sort order (lexicographic by
forward-slash relative path), and the same separator bytes around each
path/content pair. A collector reports the sha of whatever mapping data it is
actually running - embedded at build time, or a live directory an operator
pointed it at - and the backend computes this over its own copy of
contracts/mappings to compare against it. A mismatch means the collector is
polling with mapping data the platform does not recognise, which a version
string alone would not catch on a hot-patched build.

    python contracts/mapping_bundle.py            # print the current digest
"""

from __future__ import annotations

import hashlib
from pathlib import Path


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


if __name__ == "__main__":
    root = Path(__file__).resolve().parent / "mappings"
    print(bundle_sha(root))
