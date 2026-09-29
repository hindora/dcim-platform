"""Seal a device credential to one collector's own key (docs/26 Phase 4).

Before this, `GET /collector/assignments` decrypted every device credential
server-side and handed the plaintext to whichever collector asked, protected
only by TLS in transit - see app/services/collector.py's own docstring on
why returning them at all is unavoidable. TLS termination, request logging
on anything sitting in front of the API, or a misconfigured proxy are all
places a plaintext secret in that response body would be exposed that this
does not fix by itself, but sealing to the specific collector's own key
means none of those places can read it even when they can see the response -
only the collector holding the matching private key can.

This is deliberately NOT libsodium/NaCl's crypto_box_seal byte-for-byte -
building that exact construction would mean either adding PyNaCl as a new
dependency for one function, or hand-porting libsodium's blake2b-based
nonce derivation. What is built instead has the same anonymous-sender
security property NaCl's sealed box exists for, from primitives already in
this project's `cryptography` dependency: an ephemeral X25519 keypair, ECDH
against the collector's long-term public key, HKDF-SHA256 to derive an
AES-256-GCM key, then one AEAD-sealed message. The Go collector's
internal/sealedbox package is the matching decoder and the two must be
changed together - see that package's own docstring.

Wire format (all fixed-length, no framing needed):
    ephemeral_pubkey(32) || nonce(12) || ciphertext_with_tag(N)
base64-encoded for JSON transport as AssignmentCredential.sealed_b64.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

_HKDF_INFO = b"dcim-credential-seal-v1"
_NONCE_BYTES = 12
_PUBKEY_BYTES = 32


class SealError(ValueError):
    """A collector's stored encryption_pubkey is missing or malformed."""


def _derive_key(shared: bytes, ephemeral_pub: bytes, recipient_pub: bytes) -> bytes:
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32,
                salt=ephemeral_pub + recipient_pub, info=_HKDF_INFO)
    return hkdf.derive(shared)


def seal_for_collector(payload: dict[str, Any], collector_pubkey_b64: str) -> str:
    """Seal payload so only the private key matching collector_pubkey_b64
    can read it. Returns base64 ciphertext, ready for AssignmentCredential.
    sealed_b64.

    Raises SealError if collector_pubkey_b64 is not a valid 32-byte X25519
    public key - a caller must decide whether that means "fall back to
    plaintext for a pre-Phase-4 collector" or "this is actually broken",
    which is a policy question this function has no business making.
    """
    try:
        raw = base64.b64decode(collector_pubkey_b64, validate=True)
    except Exception as exc:
        raise SealError(f"encryption_pubkey is not valid base64: {exc}") from None
    if len(raw) != _PUBKEY_BYTES:
        raise SealError(f"encryption_pubkey must be {_PUBKEY_BYTES} bytes, got {len(raw)}")
    try:
        recipient_pub = X25519PublicKey.from_public_bytes(raw)
    except Exception as exc:
        raise SealError(f"encryption_pubkey is not a valid X25519 key: {exc}") from None

    ephemeral_priv = X25519PrivateKey.generate()
    ephemeral_pub_bytes = ephemeral_priv.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)
    shared = ephemeral_priv.exchange(recipient_pub)
    key = _derive_key(shared, ephemeral_pub_bytes, raw)

    nonce = os.urandom(_NONCE_BYTES)
    plaintext = json.dumps(payload, separators=(",", ":")).encode()
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)

    blob = ephemeral_pub_bytes + nonce + ciphertext
    return base64.b64encode(blob).decode()


def generate_collector_keypair() -> tuple[str, str]:
    """(private_b64, public_b64) - a convenience for tests and for
    backend/scripts tooling. The Go collector generates and persists its own
    keypair at enrollment; this exists so backend tests can build a
    realistic recipient without a real collector process."""
    priv = X25519PrivateKey.generate()
    priv_b64 = base64.b64encode(
        priv.private_bytes_raw()).decode()
    pub_b64 = base64.b64encode(
        priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    return priv_b64, pub_b64


def unseal_for_test(sealed_b64: str, private_key_b64: str) -> dict[str, Any]:
    """The decoder half, for tests only - the real decoder is the Go
    collector's internal/sealedbox package, which this must stay
    byte-for-byte compatible with."""
    blob = base64.b64decode(sealed_b64)
    if len(blob) < _PUBKEY_BYTES + _NONCE_BYTES:
        raise SealError("sealed blob too short")
    ephemeral_pub_bytes = blob[:_PUBKEY_BYTES]
    nonce = blob[_PUBKEY_BYTES:_PUBKEY_BYTES + _NONCE_BYTES]
    ciphertext = blob[_PUBKEY_BYTES + _NONCE_BYTES:]

    priv = X25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    own_pub_bytes = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ephemeral_pub = X25519PublicKey.from_public_bytes(ephemeral_pub_bytes)
    shared = priv.exchange(ephemeral_pub)
    key = _derive_key(shared, ephemeral_pub_bytes, own_pub_bytes)
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
    return json.loads(plaintext)
