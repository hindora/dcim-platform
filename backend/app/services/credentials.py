"""Device credentials as an operator manages them: create, rotate, and
assign a pool's default per protocol (docs/26 Phase 4's credential sets).

Secrets never leave this module in any response - only `secret_hint`. A
payload is validated against its kind before it is sealed, because a bad
SNMPv3 credential is otherwise discovered by the collector as an
authentication failure on every device at once.

SNMPv3 fields are the collector's own (collector/internal/adapters/snmp/
usm.go): security_name, auth_protocol + auth_key, priv_protocol + priv_key.
authPriv is the norm and the default policy here; authNoPriv is accepted
because some older cards cannot encrypt, and noAuthNoPriv is refused - it is
v2c with a user name, and gives an operator a false sense of having migrated.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import credential_hint, encrypt_secret

KINDS = {"snmp_v2c": "snmp", "snmp_v3": "snmp", "http_basic": "redfish"}
#: Matched case-insensitively, the collector's spellings.
AUTH_PROTOCOLS = ("sha", "sha1", "sha224", "sha256", "sha384", "sha512", "md5")
PRIV_PROTOCOLS = ("aes", "aes128", "aes192", "aes256", "aes192c", "aes256c", "des")
#: What security baselines reject; accepted (a site may have nothing better on
#: an old card) but flagged in the hint so it shows in every list.
WEAK = {"md5", "des"}
DEFAULT_PROTOCOLS = ("snmp", "redfish", "gnmi", "modbus", "bacnet")


class CredentialError(ValueError):
    """A credential an operator sent that cannot be used, with a message fit
    to show them as-is."""


def _text(payload: dict[str, Any], key: str, *, required: bool = True, min_len: int = 1,
          max_len: int = 256) -> str:
    v = payload.get(key)
    v = "" if v is None else str(v)
    if required and not v:
        raise CredentialError(f"{key} is required")
    if v and not (min_len <= len(v) <= max_len):
        raise CredentialError(f"{key} must be {min_len}-{max_len} characters")
    return v


def validate_payload(kind: str, payload: Any) -> dict[str, Any]:
    if kind not in KINDS:
        raise CredentialError(f"kind must be one of {', '.join(KINDS)}")
    if not isinstance(payload, dict):
        raise CredentialError("secret must be an object")
    if kind == "snmp_v2c":
        return {"community": _text(payload, "community", max_len=64)}
    if kind == "http_basic":
        return {"username": _text(payload, "username", max_len=128),
                "password": _text(payload, "password")}
    # snmp_v3
    out: dict[str, Any] = {"security_name": _text(payload, "security_name", max_len=32)}
    auth = str(payload.get("auth_protocol") or "").lower()
    priv = str(payload.get("priv_protocol") or "").lower()
    if not auth or auth == "none":
        raise CredentialError(
            "auth_protocol is required: noAuthNoPriv is v2c with a user name, not a migration")
    if auth not in AUTH_PROTOCOLS:
        raise CredentialError(f"auth_protocol must be one of {', '.join(AUTH_PROTOCOLS)}")
    # RFC 3414: a passphrase shorter than 8 octets is refused by every agent.
    out["auth_protocol"] = auth
    out["auth_key"] = _text(payload, "auth_key", min_len=8)
    if priv and priv != "none":
        if priv not in PRIV_PROTOCOLS:
            raise CredentialError(f"priv_protocol must be one of {', '.join(PRIV_PROTOCOLS)}")
        out["priv_protocol"] = priv
        out["priv_key"] = _text(payload, "priv_key", min_len=8)
    ctx = payload.get("context_name")
    if ctx:
        out["context_name"] = _text(payload, "context_name", max_len=32)
    return out


def hint(kind: str, payload: dict[str, Any]) -> str:
    h = credential_hint(kind, payload)
    if kind == "snmp_v3":
        level = "authPriv" if payload.get("priv_protocol") else "authNoPriv"
        weak = sorted({payload.get("auth_protocol"), payload.get("priv_protocol")} & WEAK)
        h = (f"{h} · {level} {payload['auth_protocol'].upper()}"
             + (f"/{payload['priv_protocol'].upper()}" if payload.get("priv_protocol") else "")
             + (f" · weak: {', '.join(w.upper() for w in weak)}" if weak else ""))
    return h


async def create(session: AsyncSession, name: str, kind: str, payload: Any) -> dict[str, Any]:
    name = (name or "").strip()
    if not name or len(name) > 128:
        raise CredentialError("name must be 1-128 characters")
    clean = validate_payload(kind, payload)
    if (await session.execute(text("SELECT 1 FROM credential WHERE name = :n"),
                              {"n": name})).first():
        raise CredentialError(f"a credential named {name} already exists")
    key_id = get_settings().active_credential_key_id
    row = (await session.execute(text("""
        INSERT INTO credential (name, protocol, kind, secret_enc, secret_hint, key_id)
        VALUES (:name, CAST(:proto AS protocol_t), :kind, :blob, :hint, :key_id)
        RETURNING id::text, name, protocol::text AS protocol, kind, secret_hint
    """), {"name": name, "proto": KINDS[kind], "kind": kind,
           "blob": encrypt_secret(clean, key_id=key_id), "hint": hint(kind, clean),
           "key_id": key_id})).mappings().one()
    return dict(row)


async def rotate(session: AsyncSession, credential_id: str, payload: Any) -> dict[str, Any]:
    """Replace the secret. Every endpoint and pool using it picks the new one
    up on its collector's next assignment fetch: the assignment ETag digests
    the credential, so nothing needs touching."""
    cur = (await session.execute(text("SELECT kind FROM credential WHERE id = CAST(:id AS uuid)"),
                                 {"id": credential_id})).first()
    if cur is None:
        raise LookupError(credential_id)
    clean = validate_payload(cur[0], payload)
    key_id = get_settings().active_credential_key_id
    row = (await session.execute(text("""
        UPDATE credential SET secret_enc = :blob, secret_hint = :hint, key_id = :key_id,
               rotated_at = to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
         WHERE id = CAST(:id AS uuid)
        RETURNING id::text, name, protocol::text AS protocol, kind, secret_hint, rotated_at
    """), {"id": credential_id, "blob": encrypt_secret(clean, key_id=key_id),
           "hint": hint(cur[0], clean), "key_id": key_id})).mappings().one()
    return dict(row)


async def listing(session: AsyncSession, q: str | None = None, kind: str | None = None,
                  limit: int = 100) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT c.id::text, c.name, c.protocol::text AS protocol, c.kind, c.secret_hint,
               c.rotated_at,
               (SELECT count(*) FROM device_endpoint e WHERE e.credential_id = c.id) AS endpoints,
               (SELECT coalesce(array_agg(cp.name ORDER BY cp.name), '{}')
                  FROM collector_pool_credential pc JOIN collector_pool cp ON cp.id = pc.pool_id
                 WHERE pc.credential_id = c.id) AS default_for
          FROM credential c
         WHERE (CAST(:q AS text) IS NULL OR c.name ILIKE '%' || CAST(:q AS text) || '%')
           AND (CAST(:kind AS text) IS NULL OR c.kind = CAST(:kind AS text))
         ORDER BY (c.kind = 'snmp_v2c'), c.name
         LIMIT :limit
    """), {"q": q or None, "kind": kind or None, "limit": limit})).mappings().all()
    return [dict(r) | {"default_for": list(r["default_for"] or [])} for r in rows]


async def pool_defaults(session: AsyncSession, pool_id: str) -> dict[str, dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT pc.protocol, c.id::text AS id, c.name, c.kind, c.secret_hint
          FROM collector_pool_credential pc JOIN credential c ON c.id = pc.credential_id
         WHERE pc.pool_id = CAST(:p AS uuid)
    """), {"p": pool_id})).mappings().all()
    return {r["protocol"]: {k: r[k] for k in ("id", "name", "kind", "secret_hint")} for r in rows}


async def set_pool_defaults(session: AsyncSession, pool_id: str,
                            defaults: dict[str, str | None], actor: str) -> dict[str, Any]:
    """{protocol: credential_id or None}. None removes that protocol's default.
    A credential must be for the protocol it is set on - an SNMP credential as
    a pool's Redfish default would fail every BMC login in the pool."""
    for proto, cid in defaults.items():
        if proto not in DEFAULT_PROTOCOLS:
            raise CredentialError(f"{proto!r} is not a polled protocol")
        if cid is None:
            await session.execute(text("""
                DELETE FROM collector_pool_credential
                 WHERE pool_id = CAST(:p AS uuid) AND protocol = :proto
            """), {"p": pool_id, "proto": proto})
            continue
        row = (await session.execute(text(
            "SELECT protocol::text FROM credential WHERE id = CAST(:id AS uuid)"),
            {"id": cid})).first()
        if row is None:
            raise CredentialError(f"no credential {cid}")
        if row[0] != proto:
            raise CredentialError(f"credential {cid} is for {row[0]}, not {proto}")
        await session.execute(text("""
            INSERT INTO collector_pool_credential (pool_id, protocol, credential_id, updated_by)
            VALUES (CAST(:p AS uuid), :proto, CAST(:c AS uuid), :actor)
            ON CONFLICT (pool_id, protocol) DO UPDATE
               SET credential_id = EXCLUDED.credential_id, updated_at = now(),
                   updated_by = EXCLUDED.updated_by
        """), {"p": pool_id, "proto": proto, "c": cid, "actor": actor})
    return await pool_defaults(session, pool_id)


async def adopt_pool_default(session: AsyncSession, pool_id: str, protocol: str) -> int:
    """Hand every endpoint of `protocol` in the pool to the pool's default:
    clear their own credential, so the default resolves. The bulk action a
    site runs when it moves a network to one credential set - here, the BMS
    plane leaving 200 per-device v2c communities for one v3 user. Refused
    without a default, which would leave them with no credential at all."""
    from app.repositories.pools import _RESOLVED_POOL

    if protocol not in (await pool_defaults(session, pool_id)):
        raise CredentialError(f"the pool has no default {protocol} credential to adopt")
    result = await session.execute(text(f"""
        UPDATE device_endpoint SET credential_id = NULL, updated_at = now()
         WHERE id IN (
            SELECT e.id
              FROM device_endpoint e
              JOIN device d        ON d.id = e.device_id
              LEFT JOIN rack rk    ON rk.id = d.rack_id
              LEFT JOIN rack_row rr ON rr.id = rk.row_id
              LEFT JOIN room rm    ON rm.id = COALESCE(rr.room_id, d.room_id)
              LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
             WHERE e.protocol::text = :proto AND e.credential_id IS NOT NULL
               AND ({_RESOLVED_POOL}) = CAST(:pool AS uuid))
    """), {"proto": protocol, "pool": pool_id})
    return result.rowcount or 0
