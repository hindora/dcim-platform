"""Key rotation against a real database (docs/26 Phase 4).

Skipped unless DCIM_TEST_DATABASE_URL is set. Every test opens a transaction
and rolls it back, so this is safe to run against a real, already-migrated
(0087+) platform database - see tests/test_collector_enrollment_live.py for
the same convention.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from rotate_credential_key import rotate

from app.core.config import get_settings
from app.core.security import decrypt_secret, encrypt_secret

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


def _new_ring_key_b64() -> str:
    return base64.b64encode(os.urandom(32)).decode()


async def _insert_credential(session, name: str, payload: dict, key_id: str | None,
                             settings) -> str:
    blob = encrypt_secret(payload, key_id=key_id, settings=settings)
    row = (await session.execute(text("""
        INSERT INTO credential (name, protocol, kind, secret_enc, key_id)
        VALUES (:name, 'snmp', 'snmp_v2c', :blob, :key_id)
        RETURNING id::text
    """), {"name": name, "blob": blob, "key_id": key_id})).scalar_one()
    return row


async def test_rotate_re_wraps_a_legacy_row_onto_a_ring_key(session):
    base = get_settings()
    new_id = "test-ring-key"
    settings = base.model_copy(update={
        "credential_key_ring": {new_id: SecretStr(_new_ring_key_b64())},
    })

    cred_id = await _insert_credential(
        session, "rotate-test-legacy", {"community": "rotate-me"}, None, settings)
    await session.flush()

    result = await rotate(session, settings, target_key_id=new_id,
                          commit_every_batch=False)
    assert cred_id not in result.failed_ids
    assert result.migrated >= 1

    row = (await session.execute(text(
        "SELECT secret_enc, key_id FROM credential WHERE id = :id"),
        {"id": cred_id})).mappings().one()
    assert row["key_id"] == new_id
    assert decrypt_secret(bytes(row["secret_enc"]), key_id=new_id,
                          settings=settings) == {"community": "rotate-me"}


async def test_rotate_skips_a_row_already_at_the_target_key(session):
    base = get_settings()
    new_id = "test-ring-key-2"
    settings = base.model_copy(update={
        "credential_key_ring": {new_id: SecretStr(_new_ring_key_b64())},
    })

    cred_id = await _insert_credential(
        session, "rotate-test-already-there", {"community": "unchanged"}, new_id, settings)
    await session.flush()

    row_before = (await session.execute(text(
        "SELECT secret_enc FROM credential WHERE id = :id"),
        {"id": cred_id})).scalar_one()

    result = await rotate(session, settings, target_key_id=new_id,
                          commit_every_batch=False)
    assert result.skipped_already_current >= 1

    row_after = (await session.execute(text(
        "SELECT secret_enc FROM credential WHERE id = :id"),
        {"id": cred_id})).scalar_one()
    assert bytes(row_before) == bytes(row_after), \
        "a row already at the target key_id must not be re-encrypted"


async def test_rotate_leaves_an_undecryptable_row_untouched_and_reports_it(session):
    base = get_settings()
    # A key_id that names nothing in the ring at all - the row this creates
    # is exactly what a partially-completed prior rotation, or a stale ring,
    # would leave behind.
    settings = base.model_copy(update={"credential_key_ring": {}})
    orphan_key_id = "test-key-that-does-not-exist-in-the-ring"

    # Insert directly with a bogus key_id/blob pair - _insert_credential
    # would itself fail to encrypt under a key that is not in the ring, so
    # this writes a plausible-shaped but deliberately undecryptable row by
    # hand, the same shape a stale ring produces for real.
    fake_blob = os.urandom(44)
    cred_id = (await session.execute(text("""
        INSERT INTO credential (name, protocol, kind, secret_enc, key_id)
        VALUES ('rotate-test-orphan', 'snmp', 'snmp_v2c', :blob, :key_id)
        RETURNING id::text
    """), {"blob": fake_blob, "key_id": orphan_key_id})).scalar_one()
    await session.flush()

    result = await rotate(session, settings, target_key_id=None,
                          commit_every_batch=False)
    assert cred_id in result.failed_ids

    row = (await session.execute(text(
        "SELECT secret_enc, key_id FROM credential WHERE id = :id"),
        {"id": cred_id})).mappings().one()
    assert bytes(row["secret_enc"]) == fake_blob
    assert row["key_id"] == orphan_key_id
