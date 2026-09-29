"""The mTLS half of collector auth: headers a real nginx front door produces
(verified live against an actual TLS handshake - see docs/26 Phase 2's
notes), and every way a strong credential must NOT be let through.

The central property under test: once a request has come through the proxy
at all (the shared trust token is present and correct), its mTLS verdict is
final. It is never silently downgraded to the bearer token - see
_try_mtls's own docstring for why that would defeat the point of mTLS for
exactly the attacker it exists to stop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.core import security
from app.core.config import get_settings

TRUST_TOKEN = "proxy-shared-secret"


class _FakeRequest:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def _headers(**kw) -> dict[str, str]:
    base = {"x-dcim-proxy-token": TRUST_TOKEN}
    base.update(kw)
    return base


@pytest.fixture
def with_trust_token(monkeypatch):
    """Settings with a proxy trust token configured, as a deployment with
    the TLS front door in place would have."""
    settings = get_settings()
    monkeypatch.setattr(settings, "proxy_trust_token",
                        _SecretStrLike(TRUST_TOKEN))
    return settings


class _SecretStrLike:
    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


@pytest.fixture
def cert_row(monkeypatch):
    """Stand in for the collector_instance row _current_cert reads."""
    table: dict[str, dict] = {}

    async def fake(collector_id: str):
        return table.get(collector_id)

    monkeypatch.setattr(security, "_current_cert", fake)
    security._cert_cache.clear()
    return table


def _valid_row(serial: str = "abc123", **overrides) -> dict:
    row = {"cert_serial": serial,
          "cert_not_after": datetime.now(UTC) + timedelta(days=20),
          "cert_revoked_at": None, "state": "active"}
    row.update(overrides)
    return row


# --- when mTLS is not in play at all --------------------------------------

async def test_no_trust_token_configured_is_the_dev_default(monkeypatch):
    """A deployment with NO proxy in front of it - the ordinary dev
    checkout - must not attempt mTLS at all, whatever headers arrive."""
    settings = get_settings()
    monkeypatch.setattr(settings, "proxy_trust_token", None)
    req = _FakeRequest(_headers(**{"x-dcim-client-verify": "SUCCESS"}))
    assert await security._try_mtls(req, settings) is None


async def test_a_request_that_did_not_come_through_the_proxy_falls_through(
        with_trust_token):
    req = _FakeRequest({"x-dcim-client-verify": "SUCCESS",
                        "x-dcim-client-cn": "CN=col-x"})  # no trust token
    assert await security._try_mtls(req, with_trust_token) is None


async def test_a_wrong_trust_token_falls_through_not_through(with_trust_token):
    req = _FakeRequest(_headers(**{"x-dcim-client-verify": "SUCCESS"}))
    req.headers["x-dcim-proxy-token"] = "not-the-real-token"
    assert await security._try_mtls(req, with_trust_token) is None


async def test_no_client_certificate_presented_falls_through_to_bearer(
        with_trust_token):
    """verify=NONE is what nginx forwards when a collector connects with no
    client cert at all - a not-yet-enrolled collector."""
    req = _FakeRequest(_headers(**{"x-dcim-client-verify": "NONE"}))
    assert await security._try_mtls(req, with_trust_token) is None


# --- a failed attempt is never downgraded -----------------------------

async def test_a_certificate_the_proxy_could_not_verify_is_a_hard_401(
        with_trust_token):
    req = _FakeRequest(_headers(**{"x-dcim-client-verify": "FAILED:unknown issuer"}))
    with pytest.raises(HTTPException) as exc:
        await security._try_mtls(req, with_trust_token)
    assert exc.value.status_code == 401


async def test_success_with_no_cn_is_refused(with_trust_token):
    req = _FakeRequest(_headers(**{"x-dcim-client-verify": "SUCCESS",
                                   "x-dcim-client-cn": ""}))
    with pytest.raises(HTTPException):
        await security._try_mtls(req, with_trust_token)


async def test_a_collector_with_no_certificate_on_file_is_refused(
        with_trust_token, cert_row):
    req = _FakeRequest(_headers(**{"x-dcim-client-verify": "SUCCESS",
                                   "x-dcim-client-cn": "CN=col-unknown",
                                   "x-dcim-client-serial": "abc"}))
    with pytest.raises(HTTPException, match="no certificate on file"):
        await security._try_mtls(req, with_trust_token)


async def test_a_decommissioned_collector_is_refused(with_trust_token, cert_row):
    cert_row["col-gone"] = _valid_row(state="decommissioned")
    req = _FakeRequest(_headers(**{"x-dcim-client-verify": "SUCCESS",
                                   "x-dcim-client-cn": "CN=col-gone",
                                   "x-dcim-client-serial": "abc123"}))
    with pytest.raises(HTTPException, match="decommissioned"):
        await security._try_mtls(req, with_trust_token)


async def test_a_revoked_certificate_is_refused(with_trust_token, cert_row):
    cert_row["col-revoked"] = _valid_row(cert_revoked_at=datetime.now(UTC))
    req = _FakeRequest(_headers(**{"x-dcim-client-verify": "SUCCESS",
                                   "x-dcim-client-cn": "CN=col-revoked",
                                   "x-dcim-client-serial": "abc123"}))
    with pytest.raises(HTTPException, match="revoked"):
        await security._try_mtls(req, with_trust_token)


async def test_a_stale_certificate_from_before_a_renewal_is_refused(
        with_trust_token, cert_row):
    """The presented serial no longer matches what renewal made current -
    not necessarily an attack, but not authorization either."""
    cert_row["col-stale"] = _valid_row(serial="new-serial-after-renewal")
    req = _FakeRequest(_headers(**{"x-dcim-client-verify": "SUCCESS",
                                   "x-dcim-client-cn": "CN=col-stale",
                                   "x-dcim-client-serial": "old-serial"}))
    with pytest.raises(HTTPException, match="not the current one"):
        await security._try_mtls(req, with_trust_token)


async def test_an_expired_certificate_is_refused(with_trust_token, cert_row):
    cert_row["col-expired"] = _valid_row(
        cert_not_after=datetime.now(UTC) - timedelta(days=1))
    req = _FakeRequest(_headers(**{"x-dcim-client-verify": "SUCCESS",
                                   "x-dcim-client-cn": "CN=col-expired",
                                   "x-dcim-client-serial": "abc123"}))
    with pytest.raises(HTTPException, match="expired"):
        await security._try_mtls(req, with_trust_token)


# --- the success path, with real nginx-shaped headers ----------------------

async def test_a_verified_current_certificate_returns_the_collector_id(
        with_trust_token, cert_row):
    cert_row["col-dc2-oob"] = _valid_row(serial="36f4343faf06654d35c6028845f59e00a9271fea")
    # The exact header shape a real nginx front door produced against a real
    # certificate issued by app/services/ca.py in this session's live proof:
    # $ssl_client_s_dn -> "CN=col-dc2-oob", $ssl_client_serial -> uppercase hex.
    req = _FakeRequest(_headers(**{
        "x-dcim-client-verify": "SUCCESS",
        "x-dcim-client-cn": "CN=col-dc2-oob",
        "x-dcim-client-serial": "36F4343FAF06654D35C6028845F59E00A9271FEA",
    }))
    assert await security._try_mtls(req, with_trust_token) == "col-dc2-oob"


async def test_serial_comparison_ignores_case_and_leading_zeros(
        with_trust_token, cert_row):
    cert_row["col-x"] = _valid_row(serial="00ab")
    req = _FakeRequest(_headers(**{"x-dcim-client-verify": "SUCCESS",
                                   "x-dcim-client-cn": "CN=col-x",
                                   "x-dcim-client-serial": "AB"}))
    assert await security._try_mtls(req, with_trust_token) == "col-x"


# --- _cn_from_dn -------------------------------------------------------

@pytest.mark.parametrize("dn,expected", [
    ("CN=col-dc2-oob", "col-dc2-oob"),
    ("", ""),
    ("CN=col-x,O=Something Else", "col-x"),
    ("O=Something Else", ""),
])
def test_cn_from_dn(dn, expected):
    assert security._cn_from_dn(dn) == expected


# --- require_collector / require_collector_cert integration ---------------

async def test_require_collector_prefers_a_verified_mtls_identity(
        with_trust_token, cert_row):
    cert_row["col-dc2-oob"] = _valid_row()
    req = _FakeRequest(_headers(**{"x-dcim-client-verify": "SUCCESS",
                                   "x-dcim-client-cn": "CN=col-dc2-oob",
                                   "x-dcim-client-serial": "abc123"}))
    identity = await security.require_collector(req, None, with_trust_token)
    assert identity == "col-dc2-oob"


async def test_require_collector_falls_through_to_bearer_when_no_cert_offered(
        with_trust_token, monkeypatch):
    async def fake_honoured(collector_id: str):
        return 0, "active"  # generation 0, active - matches the minted token

    monkeypatch.setattr(security, "_honoured", fake_honoured)
    req = _FakeRequest(_headers(**{"x-dcim-client-verify": "NONE"}))
    token = security.mint_collector_token("col-legacy", with_trust_token)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    identity = await security.require_collector(req, creds, with_trust_token)
    assert identity == "col-legacy"


async def test_require_collector_cert_refuses_a_bearer_only_request(
        with_trust_token):
    req = _FakeRequest(_headers(**{"x-dcim-client-verify": "NONE"}))
    with pytest.raises(HTTPException) as exc:
        await security.require_collector_cert(req, with_trust_token)
    assert exc.value.status_code == 401
    assert "cannot renew" in exc.value.detail
