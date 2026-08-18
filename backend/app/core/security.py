"""Credential encryption, JWT issuing/validation, and auth dependencies.

Device credentials are encrypted at rest with AES-256-GCM. The key lives in
DCIM_CREDENTIAL_KEY and never reaches the database. The only part of a secret
any user-facing API may return is ``secret_hint``.
"""

from __future__ import annotations

import base64
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


async def require_collector(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_settings),
) -> str:
    """Collector-scoped auth.

    A distinct credential type from user JWTs: the assignments endpoint returns
    decrypted device credentials, so it must never be reachable with a token
    that a browser holds.
    """
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing collector token")
    import hmac

    expected = settings.collector_token.get_secret_value()
    if not hmac.compare_digest(creds.credentials, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid collector token")
    return "collector"
