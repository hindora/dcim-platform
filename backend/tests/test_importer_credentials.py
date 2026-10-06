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
    assert "None if await self._pool_has_default(spec, " in body


def test_a_relabelled_port_is_renamed_in_place_before_the_upsert():
    """Same index, new name = the same port relabelled (ifName changes, ifIndex
    stays). The upsert keys on the name, so without a rename first it met the old
    row on (device_id, if_index) and the whole import failed - on the 23 servers
    whose BMC port went iLO/XCC -> IPMI when their vendor was corrected."""
    start = SRC.index("async def _upsert_terminations(")
    body = SRC[start:SRC.index("INSERT INTO interface", start)]
    assert "UPDATE interface SET name = :name" in body
    assert "if_index = :idx" in body and "NOT EXISTS" in body


def test_a_new_device_outside_a_scoped_default_gets_its_own_credential():
    """A new IT-OOB BMC is outside the network gear's v3 scope (migration 0098):
    the importer must pin its per-device v2c rather than leave it to a default
    it does not speak."""
    assert '_pool_has_default(spec, dev.get("device_type"))' in SRC
    start = SRC.index("async def _pool_has_default(")
    body = SRC[start:SRC.index("async def _credential_id(", start)]
    assert "pc.device_types IS NULL OR CAST(:dtype AS text) = ANY(pc.device_types)" in body
