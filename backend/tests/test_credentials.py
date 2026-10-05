"""Credential validation (docs/26 Phase 4): what an operator may store, in the
collector's own field names, before it is sealed and sent to every poller."""

from __future__ import annotations

import pytest

from app.services import credentials as c

V3 = {"security_name": "dcim-poll", "auth_protocol": "SHA256", "auth_key": "authpass123",
      "priv_protocol": "AES128", "priv_key": "privpass123"}


def test_an_authpriv_v3_credential_is_kept_in_the_collectors_field_names():
    out = c.validate_payload("snmp_v3", V3)
    assert out == {"security_name": "dcim-poll", "auth_protocol": "sha256",
                   "auth_key": "authpass123", "priv_protocol": "aes128", "priv_key": "privpass123"}


def test_authnopriv_is_accepted_for_cards_that_cannot_encrypt():
    out = c.validate_payload("snmp_v3", {**V3, "priv_protocol": "none", "priv_key": ""})
    assert "priv_protocol" not in out and "priv_key" not in out


@pytest.mark.parametrize("bad, why", [
    ({**V3, "auth_protocol": "none"}, "noAuthNoPriv"),
    ({**V3, "auth_protocol": ""}, "noAuthNoPriv"),
    ({**V3, "auth_protocol": "sha3"}, "auth_protocol must be"),
    ({**V3, "auth_key": "short"}, "auth_key must be"),
    ({**V3, "priv_key": "1234567"}, "priv_key must be"),
    ({**V3, "priv_protocol": "3des"}, "priv_protocol must be"),
    ({k: v for k, v in V3.items() if k != "security_name"}, "security_name is required"),
])
def test_a_v3_credential_no_agent_would_accept_is_refused(bad, why):
    with pytest.raises(c.CredentialError, match=why):
        c.validate_payload("snmp_v3", bad)


def test_v2c_and_http_basic_keep_only_their_own_fields():
    assert c.validate_payload("snmp_v2c", {"community": "x", "junk": 1}) == {"community": "x"}
    assert c.validate_payload("http_basic", {"username": "u", "password": "p"}) == {
        "username": "u", "password": "p"}
    with pytest.raises(c.CredentialError):
        c.validate_payload("snmp_v2c", {})
    with pytest.raises(c.CredentialError, match="kind must be"):
        c.validate_payload("snmp_v4", {})


def test_the_hint_names_the_level_and_flags_weak_protocols_but_never_a_key():
    h = c.hint("snmp_v3", c.validate_payload("snmp_v3", V3))
    assert "authPriv SHA256/AES128" in h and "authpass123" not in h and "privpass123" not in h
    weak = c.hint("snmp_v3", c.validate_payload(
        "snmp_v3", {**V3, "auth_protocol": "md5", "priv_protocol": "des"}))
    assert "weak: DES, MD5" in weak
