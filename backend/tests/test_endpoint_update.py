"""Applying an endpoint edit: what reaches the row, and what reaches nobody.

The service checks edits against the STORED row rather than the request,
because the rules that decide an edit are relational - whether this endpoint
sits behind a gateway, whether the chosen credential speaks its protocol - and
a request carries none of that.
"""

from __future__ import annotations

import pytest

from app.services import devices as service
from app.services.endpoint_config import EndpointConfigError


class FakeRepo:
    """Enough of the repository to exercise the decisions above it."""

    def __init__(self, endpoint: dict):
        self.endpoint = endpoint
        self.written: dict | None = None
        self.credentials = {
            "cred-snmp": {"id": "cred-snmp", "protocol": "snmp", "name": "ro"},
            "cred-bmc": {"id": "cred-bmc", "protocol": "redfish", "name": "bmc"},
        }
        self.profiles = {"prof-60": {"id": "prof-60", "name": "standard"}}

    async def get_endpoint(self, _session, endpoint_id):
        return dict(self.endpoint) if endpoint_id == self.endpoint["id"] else None

    async def get_credential(self, _session, cid):
        return self.credentials.get(cid)

    async def get_poll_profile(self, _session, pid):
        return self.profiles.get(pid)

    async def update_endpoint(self, _session, endpoint_id, changes):
        self.written = changes
        return {**self.endpoint, **changes}


def endpoint(**over) -> dict:
    base = {"id": "ep-1", "device_id": "dev-1", "protocol": "snmp",
            "role": "os_agent", "address": "10.50.21.26", "port": 161,
            "addressing": {}, "credential_id": "cred-snmp",
            "poll_profile_id": "prof-60", "enabled": True,
            "admin_state": "enabled", "via_endpoint_id": None,
            "via_name": None, "device_name": "SRV01"}
    return {**base, **over}


@pytest.fixture
def repo(monkeypatch):
    def _install(ep: dict) -> FakeRepo:
        fake = FakeRepo(ep)
        monkeypatch.setattr(service, "repo", fake)
        return fake
    return _install


async def update(fake_installer, ep, changes):
    fake = fake_installer(ep)
    before, after = await service.update_endpoint(
        None, device_id=ep["device_id"], endpoint_id=ep["id"], changes=changes)
    return fake, before, after


@pytest.mark.asyncio
async def test_only_what_actually_differs_is_written(repo):
    """A save that changes nothing must not bump updated_at.

    That timestamp is what the assignment version derives from, so a no-op
    write hands every collector in the estate a fresh assignment - and the
    credentials that go with it - for a form somebody opened and closed.
    """
    ep = endpoint()
    fake, before, after = await update(repo, ep, {"port": 161, "enabled": True})
    assert fake.written is None
    assert (before, after) == ({}, {})


@pytest.mark.asyncio
async def test_a_real_change_is_written_and_audited_with_its_old_value(repo):
    ep = endpoint()
    fake, before, after = await update(repo, ep, {"port": 1161})
    assert fake.written == {"port": 1161}
    assert before == {"port": 161}      # what an investigation needs
    assert after == {"port": 1161}


@pytest.mark.asyncio
async def test_the_edit_is_judged_against_the_stored_row(repo):
    """The request says nothing about sitting behind a gateway; the row does."""
    ep = endpoint(protocol="modbus", via_endpoint_id="gw-1",
                  via_name="MOXA-DC2-EL", address="10.52.10.5")
    with pytest.raises(EndpointConfigError):
        await update(repo, ep, {"address": "10.52.10.9"})


@pytest.mark.asyncio
async def test_the_selector_behind_a_gateway_stays_editable(repo):
    ep = endpoint(protocol="modbus", via_endpoint_id="gw-1",
                  via_name="MOXA-DC2-EL", addressing={"unit_id": 4})
    fake, _, after = await update(repo, ep, {"addressing": {"unit_id": 9}})
    assert fake.written == {"addressing": {"unit_id": 9}}
    assert after["addressing"] == {"unit_id": 9}


@pytest.mark.asyncio
async def test_a_credential_for_another_protocol_is_refused(repo):
    ep = endpoint()
    with pytest.raises(EndpointConfigError):
        await update(repo, ep, {"credential_id": "cred-bmc"})


@pytest.mark.asyncio
async def test_an_unknown_credential_is_refused_rather_than_stored(repo):
    """A dangling reference would pass the foreign key check nowhere near this
    layer and surface as an endpoint that authenticates with nothing."""
    ep = endpoint()
    with pytest.raises(EndpointConfigError):
        await update(repo, ep, {"credential_id": "cred-does-not-exist"})


@pytest.mark.asyncio
async def test_clearing_a_credential_is_allowed(repo):
    """Plenty of endpoints legitimately need none - v1 reads, plain Modbus."""
    ep = endpoint()
    fake, _, _ = await update(repo, ep, {"credential_id": None})
    assert fake.written == {"credential_id": None}


@pytest.mark.asyncio
async def test_an_endpoint_on_another_device_is_not_found(repo):
    """The device id in the path is a scope, not decoration: without this check
    any endpoint in the estate could be edited through any device's URL."""
    ep = endpoint()
    fake = repo(ep)
    with pytest.raises(service.EndpointNotFoundError):
        await service.update_endpoint(None, device_id="someone-else",
                                      endpoint_id="ep-1", changes={"port": 1161})
    assert fake.written is None
