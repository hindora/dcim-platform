"""Credential encryption, JWT issuing/validation, and auth dependencies.

Device credentials are encrypted at rest with AES-256-GCM. The key lives in
DCIM_CREDENTIAL_KEY and never reaches the database. The only part of a secret
any user-facing API may return is ``secret_hint``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings

_NONCE_BYTES = 12  # GCM standard; do not change without a re-encryption migration

bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------- secrets

def encrypt_secret(payload: dict[str, Any], settings: Settings | None = None) -> bytes:
    """Encrypt a credential payload. Returns nonce || ciphertext || tag."""
    s = settings or get_settings()
    nonce = os.urandom(_NONCE_BYTES)
    aes = AESGCM(s.credential_key_bytes)
    blob = aes.encrypt(nonce, json.dumps(payload, separators=(",", ":")).encode(), None)
    return nonce + blob


def decrypt_secret(blob: bytes, settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    if len(blob) <= _NONCE_BYTES:
        raise ValueError("credential blob too short")
    aes = AESGCM(s.credential_key_bytes)
    plain = aes.decrypt(bytes(blob[:_NONCE_BYTES]), bytes(blob[_NONCE_BYTES:]), None)
    return json.loads(plain)


# Which field of a credential payload is the secret, per kind. A username is
# not a secret and is useful in a hint; a community string is the entire
# credential and must never appear in one.
_SECRET_FIELDS = ("password", "community", "passphrase", "private_key",
                  "token", "secret", "auth_key", "priv_key")


def credential_hint(kind: str, payload: dict[str, Any]) -> str:
    """A hint that identifies a credential without revealing it.

    The obvious hint for SNMP v2c - "community: <value>" - is the whole
    credential written out, and on this fleet it is stored unencrypted and
    returned to every authenticated reader by GET /devices. It looks harmless
    because the simulator's community happens to be the device IP, which is not
    a secret; against real hardware the same code publishes the real community
    string.

    So a hint says what KIND of secret exists and how long it is, never what it
    is. Non-secret identifying fields - a username, an SNMPv3 security name -
    are included, because the point of a hint is to tell two credentials apart.
    """
    parts: list[str] = []
    for label in ("username", "user", "security_name"):
        if payload.get(label):
            parts.append(f"user: {payload[label]}")
            break
    for field in _SECRET_FIELDS:
        value = payload.get(field)
        if value:
            parts.append(f"{field} ({len(str(value))} chars)")
    if not parts:
        parts.append(kind or "credential")
    return ", ".join(parts)


def hint_is_safe(hint: str | None, payload: dict[str, Any]) -> bool:
    """Does this hint avoid containing any secret value it describes?

    Used at write time and asserted in tests. A length is not a secret; the
    value is.
    """
    if not hint:
        return True
    for field in _SECRET_FIELDS:
        value = payload.get(field)
        if value and str(value) in hint:
            return False
    return True


def generate_credential_key() -> str:
    """Convenience for operators: a fresh base64 key for DCIM_CREDENTIAL_KEY."""
    return base64.b64encode(os.urandom(32)).decode()


# -------------------------------------------------------------------- JWT

ROLES = ("viewer", "operator", "admin")
_ROLE_RANK = {r: i for i, r in enumerate(ROLES)}


def issue_token(username: str, role: str, settings: Settings | None = None) -> str:
    s = settings or get_settings()
    now = datetime.now(UTC)
    claims = {
        "sub": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=s.jwt_ttl_minutes)).timestamp()),
    }
    return jwt.encode(claims, s.jwt_secret.get_secret_value(), algorithm=s.jwt_algorithm)


def decode_token(token: str, settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    return jwt.decode(token, s.jwt_secret.get_secret_value(), algorithms=[s.jwt_algorithm])


# ----------------------------------------------------------- dependencies

class Principal:
    __slots__ = ("role", "username")

    def __init__(self, username: str, role: str) -> None:
        self.username = username
        self.role = role

    def __repr__(self) -> str:  # never include a token
        return f"<Principal {self.username} role={self.role}>"


async def current_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        claims = decode_token(creds.credentials)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token expired") from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc
    return Principal(claims.get("sub", ""), claims.get("role", "viewer"))


def require_role(minimum: str):
    """Authorisation as a dependency, never an ``if`` inside a handler."""
    if minimum not in _ROLE_RANK:
        raise ValueError(f"unknown role {minimum}")

    async def _dep(p: Principal = Depends(current_principal)) -> Principal:
        if _ROLE_RANK.get(p.role, -1) < _ROLE_RANK[minimum]:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"requires role {minimum} or higher")
        return p

    return _dep


# Per-collector tokens are "<collector_id>.<hmac>", where the hmac is over the
# collector id with the master token as the key. Derived rather than stored, so
# minting one needs no provisioning table, and verifying the MAC needs no
# database round trip.
#
# The property that matters: the master secret cannot be recovered from a
# derived token, so a compromised collector's token grants that collector's
# shard and nothing else. That is the difference between one machine's
# credentials and the whole fleet's.
#
# A derived token could not be revoked, though - only the master could be
# rotated, which kills every collector at once. So a token now carries a
# GENERATION, "<collector_id>.<generation>.<hmac>" with the hmac over
# "<collector_id>:<generation>", and the collector's row holds the generation
# currently honoured. Bumping it revokes that one collector's token and no
# other. Generation 0 keeps the original two-part form, so every token minted
# before this change is still generation 0 and still valid.
_TOKEN_SEP = "."


def _token_mac(master: str, collector_id: str, generation: int) -> str:
    message = collector_id if generation == 0 else f"{collector_id}:{generation}"
    return hmac.new(master.encode(), message.encode(), hashlib.sha256).hexdigest()


def mint_collector_token(collector_id: str, settings: Settings | None = None,
                         generation: int = 0) -> str:
    """Issue a token that carries, and is bound to, one collector's identity."""
    s = settings or get_settings()
    mac = _token_mac(s.collector_token.get_secret_value(), collector_id, generation)
    if generation == 0:
        return f"{collector_id}{_TOKEN_SEP}{mac}"
    return f"{collector_id}{_TOKEN_SEP}{generation}{_TOKEN_SEP}{mac}"


def parse_collector_token(token: str, settings: Settings | None = None
                          ) -> tuple[str, int] | None:
    """The collector id and generation a token proves, or None.

    The bare master token resolves to the unscoped sentinel at generation 0 -
    see ``verify_collector_token``.
    """
    s = settings or get_settings()
    master = s.collector_token.get_secret_value()

    if _TOKEN_SEP in token:
        collector_id, _, rest = token.partition(_TOKEN_SEP)
        generation = 0
        mac = rest
        if _TOKEN_SEP in rest:
            gen_text, _, mac = rest.partition(_TOKEN_SEP)
            if not gen_text.isdigit() or gen_text.startswith("0"):
                return None
            generation = int(gen_text)
        expected = _token_mac(master, collector_id, generation)
        # compare_digest: a plain == on the mac leaks its prefix through
        # timing, and the whole point of a derived token is that it cannot be
        # guessed.
        if collector_id and hmac.compare_digest(mac, expected):
            return collector_id, generation
        return None

    if hmac.compare_digest(token, master):
        return UNSCOPED_COLLECTOR, 0
    return None


def verify_collector_token(token: str, settings: Settings | None = None) -> str | None:
    """Return the collector id a token proves, or None.

    Falls back to accepting the bare master token, which every collector
    deployed before per-collector tokens is using. That fallback is a real
    weakness - it is a fleet-wide credential - so it does not pretend to be an
    identity: it resolves to the sentinel below, which the assignments endpoint
    refuses to treat as scoped and which the audit log records as unscoped.

    This proves the MAC only. Whether the generation is still honoured is a
    database question, answered by ``require_collector``.
    """
    parsed = parse_collector_token(token, settings)
    return parsed[0] if parsed else None


# What a legacy fleet-wide token resolves to. Never a real collector id, so it
# can never satisfy a scope check by accident.
UNSCOPED_COLLECTOR = "*unscoped*"

# The honoured generation and state per collector, cached briefly. Every
# collector request would otherwise pay a query for a fact that changes when
# an admin revokes a token - a few times a year. Fifteen seconds is how long a
# revoked token can outlive its revocation, which is well inside the time it
# takes an operator to notice it still works.
_REVOCATION_TTL_S = 15.0
_revocation_cache: dict[str, tuple[float, int, str | None]] = {}


def forget_collector(collector_id: str) -> None:
    """Drop the cached generation, so a revocation in THIS process is instant."""
    _revocation_cache.pop(collector_id, None)


async def _honoured(collector_id: str) -> tuple[int, str | None]:
    """The generation the platform honours for a collector, and its state.

    A collector with no row yet has generation 0 and no state: it is allowed,
    because asking for work is how a new collector registers at all.
    """
    import time

    now = time.monotonic()
    hit = _revocation_cache.get(collector_id)
    if hit and now - hit[0] < _REVOCATION_TTL_S:
        return hit[1], hit[2]

    from sqlalchemy import text

    from app.db.session import unit_of_work

    async with unit_of_work() as session:
        row = (await session.execute(text("""
            SELECT token_generation, state FROM collector_instance WHERE id = :id
        """), {"id": collector_id})).mappings().first()
    generation, state = ((int(row["token_generation"]), row["state"])
                         if row else (0, None))
    _revocation_cache[collector_id] = (now, generation, state)
    return generation, state


async def require_collector(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_settings),
) -> str:
    """Collector-scoped auth, returning WHICH collector.

    A distinct credential type from user JWTs: the assignments endpoint returns
    decrypted device credentials, so it must never be reachable with a token
    that a browser holds.

    It used to return the constant string "collector", which meant the audit
    log could record that credentials had been handed out but not to whom -
    and "someone with the collector token pulled every credential in the fleet"
    is not an investigation, it is the start of one.

    A scoped token must also still be honoured: its generation current, and its
    collector not decommissioned. Either failure is a 401 that says which, so a
    collector locked out on purpose is told so rather than retrying a bad token
    forever.
    """
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing collector token")
    parsed = parse_collector_token(creds.credentials, settings)
    if parsed is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid collector token")
    identity, generation = parsed
    if identity == UNSCOPED_COLLECTOR:
        return identity
    honoured, state = await _honoured(identity)
    if state == "decommissioned":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            f"collector {identity} is decommissioned")
    if generation != honoured:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            f"this token for {identity} has been revoked; "
                            f"issue a new one")
    return identity
