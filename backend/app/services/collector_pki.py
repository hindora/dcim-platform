"""Enrollment: exchanging a one-time token and a CSR for a client certificate.

The state machine this owns, on top of the admin-facing `state` column
migration 0085 already added:

    created (no token)  ->  token issued  ->  enrolled  ->  renewed*  ->  revoked

A collector authenticates by certificate from the moment it first enrolls;
the bearer token from migration 0085 keeps working alongside it (see 0086's
own docstring for why this is not a hard cutover), but every NEW collector
this platform creates is meant to enroll rather than lean on the shared
secret indefinitely.
"""

from __future__ import annotations

import datetime
import hashlib
import secrets
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret, encrypt_secret
from app.services import ca

#: How long an enrollment token lives before it must be reissued. The docs/26
#: default: long enough for someone to walk a token from this screen to a
#: site's console, short enough that a token pasted into the wrong ticket has
#: mostly expired by the time anyone else reads it.
DEFAULT_TOKEN_TTL_HOURS = 24

#: A cert is renewed once this share of its lifetime remains - 2/3 through,
#: i.e. 1/3 of its life left. For the 30-day default that is a 10-day window
#: to retry a renewal before the old certificate actually expires.
RENEW_AT_FRACTION_REMAINING = 1 / 3


class EnrollmentError(ValueError):
    """A token, CSR or renewal request failed a check an operator can fix."""


# --------------------------------------------------------------------- CA

async def get_active_intermediate(session: AsyncSession) -> tuple[str, str, str]:
    """The intermediate this process signs enrollments with right now.

    Returns (cert_pem, key_pem, serial). Raises EnrollmentError if the CA has
    never been bootstrapped - see scripts/init_ca.py - which is a setup step,
    not a runtime fault, so it is reported as one rather than a 500.
    """
    row = (await session.execute(text("""
        SELECT cert_pem, key_enc, serial FROM certificate_authority
         WHERE kind = 'intermediate' AND is_active
    """))).mappings().first()
    if row is None:
        raise EnrollmentError(
            "no active intermediate CA - run scripts/init_ca.py to bootstrap one")
    key_pem = decrypt_secret(bytes(row["key_enc"]))["key_pem"]
    return row["cert_pem"], key_pem, row["serial"]


async def trust_chain(session: AsyncSession) -> list[str]:
    """Every CA certificate a client needs to build a trust path, root last.

    The root's cert (never its key) is stored here too, written once at
    bootstrap, purely so this endpoint can hand it out - collectors and the
    TLS proxy both need it to verify each other's chain, and re-typing a
    fingerprint by hand at every site is how a trust bundle drifts.
    """
    rows = (await session.execute(text("""
        SELECT cert_pem, kind FROM certificate_authority
         WHERE (kind = 'root') OR (kind = 'intermediate' AND is_active)
         ORDER BY kind = 'root'
    """))).mappings().all()
    return [r["cert_pem"] for r in rows]


async def store_root(session: AsyncSession, cert_pem: str, serial: str,
                     not_after: datetime.datetime) -> None:
    """Record the root's PUBLIC cert only - see collector_pki module docs."""
    await session.execute(text("""
        INSERT INTO certificate_authority (kind, serial, cert_pem, key_enc,
                                           not_after, is_active)
        VALUES ('root', :serial, :cert_pem, NULL, :not_after, false)
    """), {"serial": serial, "cert_pem": cert_pem, "not_after": not_after})


async def store_intermediate(session: AsyncSession, cert_pem: str, key_pem: str,
                             serial: str, not_after: datetime.datetime,
                             *, activate: bool) -> None:
    if activate:
        # One active intermediate at a time (the partial unique index in
        # migration 0086 enforces this too; deactivating first here avoids
        # relying on the constraint to surface the conflict as a 500).
        await session.execute(text("""
            UPDATE certificate_authority SET is_active = false
             WHERE kind = 'intermediate' AND is_active
        """))
    await session.execute(text("""
        INSERT INTO certificate_authority (kind, serial, cert_pem, key_enc,
                                           not_after, is_active)
        VALUES ('intermediate', :serial, :cert_pem, :key_enc, :not_after, :active)
    """), {"serial": serial, "cert_pem": cert_pem,
           "key_enc": encrypt_secret({"key_pem": key_pem}),
           "not_after": not_after, "active": activate})


# ------------------------------------------------------------ enrollment

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_enrollment_token(session: AsyncSession, collector_id: str,
                                 actor: str,
                                 ttl_hours: int = DEFAULT_TOKEN_TTL_HOURS
                                 ) -> tuple[str, datetime.datetime]:
    """A single-use token bound to one collector row. Returns (token, expiry).

    Only the hash is stored - the same reasoning as the bearer token's HMAC
    derivation: the database is not the secret's safe.
    """
    token = secrets.token_urlsafe(32)
    expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=ttl_hours)
    await session.execute(text("""
        INSERT INTO enrollment_token (collector_id, token_hash, expires_at, created_by)
        VALUES (:id, :hash, :expires, :actor)
    """), {"id": collector_id, "hash": _hash_token(token),
           "expires": expires_at, "actor": actor})
    return token, expires_at


async def enroll(session: AsyncSession, *, token: str, csr_pem: str,
                 encryption_pubkey: str | None) -> ca.IssuedCert:
    """Consume a token and a CSR, return a signed certificate.

    Ordering matters: the token is locked and checked for single use BEFORE
    the CSR is validated, so two concurrent enrollments racing the same
    token cannot both fall through to signing - the loser sees "already
    used", not a second valid certificate for a token that was meant to
    prove one enrollment.
    """
    row = (await session.execute(text("""
        SELECT id, collector_id, expires_at, used_at, created_by
          FROM enrollment_token WHERE token_hash = :hash
          FOR UPDATE
    """), {"hash": _hash_token(token)})).mappings().first()
    if row is None:
        raise EnrollmentError("no such enrollment token")
    if row["used_at"] is not None:
        raise EnrollmentError("this enrollment token has already been used")
    if row["expires_at"] <= datetime.datetime.now(datetime.UTC):
        raise EnrollmentError("this enrollment token has expired")

    collector_id = row["collector_id"]
    collector = (await session.execute(text("""
        SELECT state FROM collector_instance WHERE id = :id
    """), {"id": collector_id})).mappings().first()
    if collector is None or collector["state"] == "decommissioned":
        raise EnrollmentError(f"collector {collector_id} is not available to enroll")

    inter_cert, inter_key, _ = await get_active_intermediate(session)
    issued = ca.sign_collector_csr(
        csr_pem, collector_id=collector_id,
        intermediate_cert_pem=inter_cert, intermediate_key_pem=inter_key)

    await session.execute(text("""
        UPDATE enrollment_token SET used_at = now() WHERE id = :id
    """), {"id": row["id"]})
    # Whoever CREATED the token authorised this enrollment - the collector
    # itself has no operator identity of its own to record.
    await _record_cert(session, collector_id, issued, encryption_pubkey,
                       enrolled_by=row["created_by"], first_enroll=True)
    return issued


async def renew(session: AsyncSession, *, collector_id: str, csr_pem: str
                ) -> ca.IssuedCert:
    """Issue a fresh certificate to a collector that already holds a valid
    one - authorization is the caller having proven that identity over mTLS
    before this is ever reached, not anything checked in here."""
    collector = (await session.execute(text("""
        SELECT state, cert_revoked_at FROM collector_instance WHERE id = :id
    """), {"id": collector_id})).mappings().first()
    if collector is None or collector["state"] == "decommissioned":
        raise EnrollmentError(f"collector {collector_id} is not available to renew")
    if collector["cert_revoked_at"] is not None:
        raise EnrollmentError(f"collector {collector_id}'s certificate was revoked; "
                              f"it must re-enroll with a new token")

    inter_cert, inter_key, _ = await get_active_intermediate(session)
    issued = ca.sign_collector_csr(
        csr_pem, collector_id=collector_id,
        intermediate_cert_pem=inter_cert, intermediate_key_pem=inter_key)
    await _record_cert(session, collector_id, issued, encryption_pubkey=None,
                       enrolled_by=None, first_enroll=False)
    return issued


async def _record_cert(session: AsyncSession, collector_id: str, issued: ca.IssuedCert,
                       encryption_pubkey: str | None, enrolled_by: str | None,
                       *, first_enroll: bool) -> None:
    # Overwriting cert_serial IS the revocation of whatever serial was there
    # before: the per-request check (app/core/security.py's mTLS path)
    # compares a presented certificate's serial against this column, so the
    # old one simply stops being the one that is current the moment this
    # commits. No separate revoke step, no CRL.
    params: dict[str, Any] = {
        "id": collector_id, "serial": issued.serial,
        "fp": issued.fingerprint_sha256, "not_after": issued.not_after,
    }
    sets = ["cert_serial = :serial", "cert_fingerprint_sha256 = :fp",
            "cert_not_after = :not_after", "cert_revoked_at = NULL"]
    if first_enroll:
        sets += ["enrolled_at = now()", "enrolled_by = :enrolled_by"]
        params["enrolled_by"] = enrolled_by
    if encryption_pubkey is not None:
        sets.append("encryption_pubkey = :pubkey")
        params["pubkey"] = encryption_pubkey
    await session.execute(
        text(f"UPDATE collector_instance SET {', '.join(sets)} WHERE id = :id"),
        params)


async def revoke_cert(session: AsyncSession, collector_id: str) -> bool:
    """Stop trusting this collector's current certificate without
    decommissioning the row - for a suspected key compromise, where the
    collector should re-enroll (a fresh token) rather than be retired."""
    result = await session.execute(text("""
        UPDATE collector_instance SET cert_revoked_at = now()
         WHERE id = :id AND cert_serial IS NOT NULL AND cert_revoked_at IS NULL
    """), {"id": collector_id})
    return bool(result.rowcount)


async def cert_status(session: AsyncSession, collector_id: str) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        SELECT cert_serial, cert_fingerprint_sha256, cert_not_after,
               cert_revoked_at, enrolled_at, enrolled_by
          FROM collector_instance WHERE id = :id
    """), {"id": collector_id})).mappings().first()
    return dict(row) if row else None


def renewal_due(not_after: datetime.datetime, issued_lifetime_days: int
                ) -> bool:
    """True once less than 1/3 of the certificate's life remains.

    A pure function of the two numbers a caller already has, so the
    threshold is testable without a clock or a database.
    """
    remaining = not_after - datetime.datetime.now(datetime.UTC)
    total = datetime.timedelta(days=issued_lifetime_days)
    return remaining <= total * RENEW_AT_FRACTION_REMAINING
