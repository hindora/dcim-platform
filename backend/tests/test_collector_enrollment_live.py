"""Enrollment against the real database, with the real bootstrapped CA.

Skipped unless DCIM_TEST_DATABASE_URL points at a database that has already
been through scripts/init_ca.py - CI's migrations job stands up a fresh
Postgres with no CA bootstrapped, so this is a manual/local gate, the same
role test_correlation_live.py plays for the alarm engine.

    DCIM_TEST_DATABASE_URL=postgresql+asyncpg://... \
        pytest tests/test_collector_enrollment_live.py -v

Every test opens a transaction and rolls it back, so it is safe to run
against a real, already-bootstrapped platform database.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.repositories import collector as collector_repo
from app.services import ca, collector_pki, sealed_credential

DB_URL = os.getenv("DCIM_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not DB_URL, reason="set DCIM_TEST_DATABASE_URL to run"),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(DB_URL, poolclass=None)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        trans = await s.begin()
        try:
            yield s
        finally:
            await trans.rollback()
            await engine.dispose()


async def _make_collector(s, collector_id: str, state: str = "active") -> None:
    await s.execute(text("""
        INSERT INTO collector_instance (id, last_heartbeat, endpoints_owned,
                                        endpoints_online, status, stats, state,
                                        token_generation, state_changed_at,
                                        state_changed_by)
        VALUES (:id, '-infinity', 0, 0, 'UNKNOWN', '{}'::jsonb, :state, 1,
                now(), 'test')
    """), {"id": collector_id, "state": state})


async def test_the_bootstrapped_ca_is_actually_there(session):
    cert, key, serial = await collector_pki.get_active_intermediate(session)
    assert "BEGIN CERTIFICATE" in cert
    assert "BEGIN PRIVATE KEY" in key
    assert serial


async def test_a_full_enroll_issues_a_certificate_that_chains_to_the_root(session):
    await _make_collector(session, "col-test-enroll-a")
    token, _expires = await collector_pki.issue_enrollment_token(
        session, "col-test-enroll-a", actor="test-admin")

    key = ec.generate_private_key(ca.CURVE)
    csr = ca.build_csr("col-test-enroll-a", key)
    issued = await collector_pki.enroll(
        session, token=token, csr_pem=csr, encryption_pubkey="deadbeef")

    chain = await collector_pki.trust_chain(session)
    root_pem = chain[-1]
    intermediate_pem, _key, _serial = await collector_pki.get_active_intermediate(session)
    assert ca.verify_chain(issued.cert_pem, intermediate_pem, root_pem)

    status = await collector_pki.cert_status(session, "col-test-enroll-a")
    assert status["cert_serial"] == issued.serial
    assert status["enrolled_by"] == "test-admin"
    assert status["enrolled_at"] is not None


async def test_a_real_enrolled_pubkey_round_trips_and_can_seal_a_credential(session):
    """docs/26 Phase 4 against real Postgres, not a mock: enroll with a real
    X25519 public key, read it back through app/repositories/collector.py's
    encryption_pubkey (what build_assignment actually calls), and confirm a
    credential sealed against that stored value unseals to the original
    payload. This is the one link the Go-side and Python-side unit/interop
    tests cannot cover by themselves - that a key genuinely round-tripping
    through the enrollment table is usable, not just a value sitting in a
    Python variable."""
    await _make_collector(session, "col-test-seal-roundtrip")
    token, _ = await collector_pki.issue_enrollment_token(
        session, "col-test-seal-roundtrip", actor="test-admin")

    priv_b64, pub_b64 = sealed_credential.generate_collector_keypair()
    key = ec.generate_private_key(ca.CURVE)
    csr = ca.build_csr("col-test-seal-roundtrip", key)
    await collector_pki.enroll(session, token=token, csr_pem=csr,
                              encryption_pubkey=pub_b64)

    stored = await collector_repo.encryption_pubkey(session, "col-test-seal-roundtrip")
    assert stored == pub_b64

    sealed = sealed_credential.seal_for_collector(
        {"username": "admin", "password": "hunter2"}, stored)
    assert sealed_credential.unseal_for_test(sealed, priv_b64) == {
        "username": "admin", "password": "hunter2"}


async def test_a_never_enrolled_collector_has_no_encryption_pubkey(session):
    await _make_collector(session, "col-test-no-seal-key")
    stored = await collector_repo.encryption_pubkey(session, "col-test-no-seal-key")
    assert stored is None


async def test_a_token_cannot_be_used_twice(session):
    await _make_collector(session, "col-test-single-use")
    token, _ = await collector_pki.issue_enrollment_token(
        session, "col-test-single-use", actor="test-admin")
    key = ec.generate_private_key(ca.CURVE)
    csr = ca.build_csr("col-test-single-use", key)
    await collector_pki.enroll(session, token=token, csr_pem=csr,
                               encryption_pubkey=None)

    with pytest.raises(collector_pki.EnrollmentError, match="already been used"):
        await collector_pki.enroll(session, token=token, csr_pem=csr,
                                   encryption_pubkey=None)


async def test_an_expired_token_is_refused(session):
    await _make_collector(session, "col-test-expired")
    token, _ = await collector_pki.issue_enrollment_token(
        session, "col-test-expired", actor="test-admin", ttl_hours=24)
    # Force it into the past directly - issue_enrollment_token has no
    # negative-ttl escape hatch, on purpose.
    await session.execute(text("""
        UPDATE enrollment_token SET expires_at = now() - interval '1 minute'
         WHERE collector_id = 'col-test-expired'
    """))
    key = ec.generate_private_key(ca.CURVE)
    csr = ca.build_csr("col-test-expired", key)
    with pytest.raises(collector_pki.EnrollmentError, match="expired"):
        await collector_pki.enroll(session, token=token, csr_pem=csr,
                                   encryption_pubkey=None)


async def test_a_token_for_one_collector_cannot_enroll_a_different_id(session):
    await _make_collector(session, "col-test-owner")
    token, _ = await collector_pki.issue_enrollment_token(
        session, "col-test-owner", actor="test-admin")
    key = ec.generate_private_key(ca.CURVE)
    csr = ca.build_csr("col-test-impersonator", key)
    with pytest.raises(ca.CAError, match="does not match"):
        await collector_pki.enroll(session, token=token, csr_pem=csr,
                                   encryption_pubkey=None)


async def test_a_decommissioned_collector_cannot_be_enrolled(session):
    await _make_collector(session, "col-test-decommissioned", state="decommissioned")
    token, _ = await collector_pki.issue_enrollment_token(
        session, "col-test-decommissioned", actor="test-admin")
    key = ec.generate_private_key(ca.CURVE)
    csr = ca.build_csr("col-test-decommissioned", key)
    with pytest.raises(collector_pki.EnrollmentError, match="not available"):
        await collector_pki.enroll(session, token=token, csr_pem=csr,
                                   encryption_pubkey=None)


async def test_renewal_issues_a_new_serial_and_the_old_one_stops_being_current(session):
    await _make_collector(session, "col-test-renew")
    token, _ = await collector_pki.issue_enrollment_token(
        session, "col-test-renew", actor="test-admin")
    key = ec.generate_private_key(ca.CURVE)
    csr = ca.build_csr("col-test-renew", key)
    first = await collector_pki.enroll(session, token=token, csr_pem=csr,
                                       encryption_pubkey=None)

    new_key = ec.generate_private_key(ca.CURVE)
    new_csr = ca.build_csr("col-test-renew", new_key)
    renewed = await collector_pki.renew(session, collector_id="col-test-renew",
                                        csr_pem=new_csr)

    assert renewed.serial != first.serial
    status = await collector_pki.cert_status(session, "col-test-renew")
    assert status["cert_serial"] == renewed.serial


async def test_a_revoked_collector_cannot_renew_and_must_reenroll(session):
    await _make_collector(session, "col-test-revoke-renew")
    token, _ = await collector_pki.issue_enrollment_token(
        session, "col-test-revoke-renew", actor="test-admin")
    key = ec.generate_private_key(ca.CURVE)
    csr = ca.build_csr("col-test-revoke-renew", key)
    await collector_pki.enroll(session, token=token, csr_pem=csr,
                               encryption_pubkey=None)

    revoked = await collector_pki.revoke_cert(session, "col-test-revoke-renew")
    assert revoked

    with pytest.raises(collector_pki.EnrollmentError, match="revoked"):
        await collector_pki.renew(session, collector_id="col-test-revoke-renew",
                                  csr_pem=csr)

    # But a fresh token lets it enroll again from scratch.
    token2, _ = await collector_pki.issue_enrollment_token(
        session, "col-test-revoke-renew", actor="test-admin")
    new_key = ec.generate_private_key(ca.CURVE)
    new_csr = ca.build_csr("col-test-revoke-renew", new_key)
    reissued = await collector_pki.enroll(session, token=token2, csr_pem=new_csr,
                                          encryption_pubkey=None)
    status = await collector_pki.cert_status(session, "col-test-revoke-renew")
    assert status["cert_serial"] == reissued.serial
    assert status["cert_revoked_at"] is None  # re-enrolling clears it


async def test_revoking_twice_is_refused_the_second_time(session):
    await _make_collector(session, "col-test-double-revoke")
    token, _ = await collector_pki.issue_enrollment_token(
        session, "col-test-double-revoke", actor="test-admin")
    key = ec.generate_private_key(ca.CURVE)
    csr = ca.build_csr("col-test-double-revoke", key)
    await collector_pki.enroll(session, token=token, csr_pem=csr,
                               encryption_pubkey=None)
    assert await collector_pki.revoke_cert(session, "col-test-double-revoke")
    assert not await collector_pki.revoke_cert(session, "col-test-double-revoke")


async def test_a_never_enrolled_collector_has_nothing_to_revoke(session):
    await _make_collector(session, "col-test-never-enrolled")
    assert not await collector_pki.revoke_cert(session, "col-test-never-enrolled")
