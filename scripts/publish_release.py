#!/usr/bin/env python3
"""Sign a collector build offline and publish it to the platform (docs/26 Phase 7).

The platform stores and serves releases but never holds the private key: this
script signs on the machine that builds the release, and each collector trusts
only the public keys in its own config. A compromised platform can therefore
withhold an upgrade, never push a binary of its own.

    # once: a signing key (keep the private half OFF the platform host)
    python scripts/publish_release.py gen-key --out ~/.dcim/release-signing.key

    # per release: sign and upload
    python scripts/publish_release.py publish --binary collector-linux \\
        --version 0.4.0 --key ~/.dcim/release-signing.key --key-id dev-2026 \\
        --server http://127.0.0.1:8000 --user admin   # password from DCIM_ADMIN_PASSWORD

The signature is Ed25519 over the 32 raw bytes of the artefact's SHA-256 -
exactly what the collector's update.Verify checks.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.request
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def gen_key(out: str) -> int:
    out = os.path.expanduser(out)
    if os.path.exists(out):
        print(f"refusing to overwrite {out}", file=sys.stderr)
        return 1
    key = Ed25519PrivateKey.generate()
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    os.chmod(out, 0o600)
    print("private key:", out)
    print("public key (put in each collector's update.trusted_keys):", public_b64(key))
    return 0


def public_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def load_key(path: str) -> Ed25519PrivateKey:
    with open(os.path.expanduser(path), "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("not an Ed25519 private key")
    return key


def _call(server: str, path: str, token: str | None, body: bytes | None = None,
          ctype: str = "application/json") -> dict:
    req = urllib.request.Request(server.rstrip("/") + "/api/v1" + path, data=body,
                                 method="POST" if body is not None else "GET")
    req.add_header("Content-Type", ctype)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read() or b"null")


def publish(a: argparse.Namespace) -> int:
    data = open(a.binary, "rb").read()
    digest = hashlib.sha256(data).digest()
    key = load_key(a.key)
    sig = base64.b64encode(key.sign(digest)).decode()
    print(f"version {a.version}: {len(data)} bytes, sha256 {digest.hex()}")
    print(f"signed by {a.key_id} ({public_b64(key)})")
    password = os.environ.get("DCIM_ADMIN_PASSWORD") or a.password
    if not password:
        raise SystemExit("set DCIM_ADMIN_PASSWORD (or --password)")
    token = _call(a.server, "/login", None,
                  json.dumps({"username": a.user, "password": password}).encode())["token"]
    bnd = uuid.uuid4().hex
    parts = []
    for name, val in (("version", a.version), ("signature", sig), ("key_id", a.key_id),
                      ("notes", a.notes or "")):
        parts.append(f"--{bnd}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                     f"{val}\r\n".encode())
    parts.append(f"--{bnd}\r\nContent-Disposition: form-data; name=\"artifact\"; "
                 f"filename=\"collector\"\r\nContent-Type: application/octet-stream\r\n\r\n"
                 .encode() + data + b"\r\n")
    parts.append(f"--{bnd}--\r\n".encode())
    result = _call(a.server, "/collectors/releases", token, b"".join(parts),
                   f"multipart/form-data; boundary={bnd}")
    print("published:", json.dumps(result))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen-key")
    g.add_argument("--out", required=True)
    pub = sub.add_parser("publish")
    pub.add_argument("--binary", required=True)
    pub.add_argument("--version", required=True)
    pub.add_argument("--key", required=True)
    pub.add_argument("--key-id", required=True)
    pub.add_argument("--server", default="http://127.0.0.1:8000")
    pub.add_argument("--user", default="admin")
    pub.add_argument("--password")
    pub.add_argument("--notes")
    a = p.parse_args()
    return gen_key(a.out) if a.cmd == "gen-key" else publish(a)


if __name__ == "__main__":
    sys.exit(main())
