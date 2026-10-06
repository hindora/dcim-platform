"""A re-import keeps an endpoint's own credential.

The IT-OOB pools moved to an SNMPv3 default for their network gear while the
310 server BMCs on the same subnets kept a per-device v2c community. The
importer's upsert wrote credential_id = EXCLUDED.credential_id, which is NULL
in a pool with a default - so re-importing would have moved every BMC onto a
v3 user it does not have. Only an explicit adopt clears an endpoint's own
credential.
"""
from __future__ import annotations

from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "app" / "importer" / "simulator.py").read_text(
    encoding="utf-8")


def _upsert() -> str:
    start = SRC.index("async def _upsert_endpoints(")
    return SRC[start:SRC.index("RETURNING id::text", start)]


def test_an_existing_own_credential_survives_a_re_import():
    body = _upsert()
    assert "credential_id = COALESCE(device_endpoint.credential_id," in body
    assert "credential_id = EXCLUDED.credential_id," not in body


def test_a_pool_default_still_means_no_pin_for_a_new_endpoint():
    """The adoption the pool-default branch protects still holds: an adopted
    endpoint has no own credential, so COALESCE leaves it on the default."""
    body = _upsert()
    assert "None if await self._pool_has_default(spec)" in body
