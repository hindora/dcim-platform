"""A credential never appears in a log, an audit row, or a response.

The exit criterion for this phase, tested at each of the three places a secret
can escape. Every one of them has the same failure mode: some code path that
means well - "log the payload we failed on", "record what changed", "return the
object" - and takes the whole object with it.
"""

from __future__ import annotations

import json

import pytest
from cryptography.exceptions import InvalidTag

from app.core import audit
from app.core.logging import _redact
from app.core.security import (
    UNSCOPED_COLLECTOR,
    credential_hint,
    decrypt_secret,
    encrypt_secret,
    hint_is_safe,
    mint_collector_token,
    verify_collector_token,
)

SECRET = "tr0ub4dor-horse-battery"


# --- logging ------------------------------------------------------------------

def test_the_log_processor_redacts_credential_shaped_keys():
    out = _redact(None, "", {"event": "poll failed", "password": SECRET,
                             "community": "10.50.1.4", "token": "abc"})
    assert SECRET not in json.dumps(out)
    assert out["community"] != "10.50.1.4"
    assert out["event"] == "poll failed"


def test_redaction_reaches_nested_structures():
    """The realistic shape: an endpoint dict logged whole on a failure path."""
    out = _redact(None, "", {"event": "assignment", "endpoint": {
        "address": "10.50.1.4",
        "credential": {"kind": "snmp_v2c", "community": SECRET}}})
    assert SECRET not in json.dumps(out)
    assert "10.50.1.4" in json.dumps(out)  # non-secret detail survives


def test_redaction_does_not_mangle_ordinary_values():
    out = _redact(None, "", {"event": "poll", "device": "SRV-DC1-01",
                             "duration_ms": 41.2})
    assert out["device"] == "SRV-DC1-01"
    assert out["duration_ms"] == 41.2


# --- audit rows ---------------------------------------------------------------

def test_audit_scrub_redacts_by_key_at_any_depth():
    scrubbed = audit.scrub({
        "name": "core-sw-1",
        "auth": {"password": SECRET},
        "endpoints": [{"community": SECRET}, {"address": "10.50.1.4"}],
    })
    body = json.dumps(scrubbed)
    assert SECRET not in body
    assert "core-sw-1" in body
    assert "10.50.1.4" in body


def test_audit_scrub_drops_bytes_entirely():
    """The one bytes column in this schema is credential.secret_enc. A
    ciphertext in an audit row is still the secret, one key away."""
    scrubbed = audit.scrub({"secret_enc": b"\x00\x01ciphertext"})
    assert scrubbed["secret_enc"] == audit.REDACTED
    assert "ciphertext" not in json.dumps(scrubbed)


def test_audit_scrub_survives_a_deeply_nested_structure():
    deep: dict = {"password": SECRET}
    for _ in range(30):
        deep = {"next": deep}
    body = json.dumps(audit.scrub(deep))
    assert SECRET not in body


def test_audit_scrub_leaves_a_credential_free_payload_intact():
    payload = {"method": "snmp", "subnets": ["10.50.0.0/16"]}
    assert audit.scrub(payload) == payload


# --- encryption at rest -------------------------------------------------------

def test_a_credential_round_trips_through_encryption(monkeypatch):
    from app.core.config import get_settings
    settings = get_settings()
    blob = encrypt_secret({"community": SECRET}, settings)
    assert SECRET.encode() not in blob          # not merely encoded
    assert decrypt_secret(blob, settings) == {"community": SECRET}


def test_the_same_secret_encrypts_differently_every_time():
    """A fresh nonce per encryption. Without it, identical communities produce
    identical ciphertext and the database leaks which devices share one."""
    from app.core.config import get_settings
    settings = get_settings()
    a = encrypt_secret({"community": SECRET}, settings)
    b = encrypt_secret({"community": SECRET}, settings)
    assert a != b
    assert decrypt_secret(a, settings) == decrypt_secret(b, settings)


def test_a_tampered_blob_is_rejected_rather_than_returning_garbage():
    """GCM authenticates. A flipped bit must fail, not decrypt to nonsense that
    then gets sent to a device as a password."""
    from app.core.config import get_settings
    settings = get_settings()
    blob = bytearray(encrypt_secret({"community": SECRET}, settings))
    blob[-1] ^= 0x01
    # InvalidTag specifically: the authentication tag failing is the guarantee
    # being tested. A blind Exception would also pass if the code raised
    # TypeError before it ever checked the tag.
    with pytest.raises(InvalidTag):
        decrypt_secret(bytes(blob), settings)


# --- collector token scoping --------------------------------------------------

def test_a_derived_token_proves_one_collector_identity():
    from app.core.config import get_settings
    settings = get_settings()
    token = mint_collector_token("col-1", settings)
    assert verify_collector_token(token, settings) == "col-1"


def test_a_token_minted_for_one_collector_does_not_verify_as_another():
    """The whole point: a compromised collector's token grants that
    collector's shard, not the fleet's credentials."""
    from app.core.config import get_settings
    settings = get_settings()
    token = mint_collector_token("col-1", settings)
    forged = "col-2" + token[token.index("."):]
    assert verify_collector_token(forged, settings) is None


def test_a_garbage_token_is_rejected():
    from app.core.config import get_settings
    settings = get_settings()
    assert verify_collector_token("col-1.deadbeef", get_settings()) is None
    assert verify_collector_token("nonsense", settings) is None
    assert verify_collector_token("", settings) is None


def test_the_legacy_fleet_wide_token_resolves_to_a_sentinel_not_an_identity():
    """Every collector deployed before this change uses the shared token, so it
    still works - but it must never satisfy a scope check, which is why it
    resolves to something that cannot be a collector id."""
    from app.core.config import get_settings
    settings = get_settings()
    master = settings.collector_token.get_secret_value()
    assert verify_collector_token(master, settings) == UNSCOPED_COLLECTOR
    assert UNSCOPED_COLLECTOR not in ("col-1", "collector")


def test_a_derived_token_does_not_reveal_the_master():
    from app.core.config import get_settings
    settings = get_settings()
    token = mint_collector_token("col-1", settings)
    assert settings.collector_token.get_secret_value() not in token


# --- hints --------------------------------------------------------------------

def test_a_hint_describes_a_secret_without_containing_it():
    """Found live: 894 stored hints read "community: 10.51.13.27", which on
    this fleet IS the community string - the one credential column that is
    neither encrypted nor withheld from GET /devices."""
    payload = {"community": "10.51.13.27"}
    hint = credential_hint("snmp_v2c", payload)
    assert "10.51.13.27" not in hint
    assert "community" in hint
    assert hint_is_safe(hint, payload)


def test_a_hint_keeps_the_username_because_a_username_is_not_a_secret():
    payload = {"username": "admin", "password": SECRET}
    hint = credential_hint("http_basic", payload)
    assert "admin" in hint          # the part that tells two credentials apart
    assert SECRET not in hint
    assert hint_is_safe(hint, payload)


def test_the_old_hint_format_is_recognised_as_unsafe():
    assert not hint_is_safe("community: 10.51.13.27", {"community": "10.51.13.27"})
