"""Two collectors must behave exactly as one would.

docs/26 Phase 0. Every test here is a way a second collector used to go wrong
silently: placed nowhere and handed another site's devices, a revoked token
that still worked, a dropped sample that raised nothing, another collector's
readings accepted as if they were the owner's.
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.alarms import platform as p
from app.core import security
from app.core.config import get_settings
from app.ingest import ownership
from app.schemas import (
    Assignment,
    AssignmentCredential,
    AssignmentEndpoint,
    AssignmentPoll,
)
from app.services import collector as collector_service
from app.services import sharding

# --- tokens -------------------------------------------------------------------


def test_generation_zero_is_the_token_every_collector_already_holds():
    """Existing derived tokens must keep working through the upgrade."""
    settings = get_settings()
    token = security.mint_collector_token("col-1", settings)
    assert token.count(".") == 1
    assert security.parse_collector_token(token, settings) == ("col-1", 0)


def test_a_later_generation_carries_its_number_and_proves_it():
    settings = get_settings()
    token = security.mint_collector_token("col-dc2", settings, generation=3)
    assert security.parse_collector_token(token, settings) == ("col-dc2", 3)
    # The number is under the MAC: editing it breaks the token rather than
    # promoting it to a generation the platform still honours.
    forged = token.replace(".3.", ".4.")
    assert security.parse_collector_token(forged, settings) is None


def test_a_generation_zero_mac_does_not_prove_a_later_generation():
    settings = get_settings()
    old = security.mint_collector_token("col-1", settings)
    mac = old.split(".", 1)[1]
    assert security.parse_collector_token(f"col-1.1.{mac}", settings) is None
    # Leading zeros would give one generation two spellings.
    assert security.parse_collector_token(f"col-1.01.{mac}", settings) is None


@pytest.fixture
def honoured(monkeypatch):
    """Stand in for the collector_instance row the check reads."""
    table: dict[str, tuple[int, str | None]] = {}

    async def fake(collector_id: str):
        return table.get(collector_id, (0, None))

    monkeypatch.setattr(security, "_honoured", fake)
    return table


class _NoMTLSRequest:
    """A request that made no mTLS attempt - no proxy headers at all - so
    require_collector falls straight through to the bearer token, the same
    as every request in a dev checkout with no TLS proxy in front of it."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


async def _auth(token: str) -> str:
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    return await security.require_collector(_NoMTLSRequest(), creds, get_settings())


async def test_a_revoked_token_is_refused_and_says_so(honoured):
    settings = get_settings()
    honoured["col-1"] = (2, "active")
    stale = security.mint_collector_token("col-1", settings, generation=1)
    with pytest.raises(HTTPException) as exc:
        await _auth(stale)
    assert exc.value.status_code == 401
    assert "revoked" in exc.value.detail
    current = security.mint_collector_token("col-1", settings, generation=2)
    assert await _auth(current) == "col-1"


async def test_a_decommissioned_collector_cannot_authenticate(honoured):
    honoured["col-old"] = (0, "decommissioned")
    token = security.mint_collector_token("col-old", get_settings())
    with pytest.raises(HTTPException) as exc:
        await _auth(token)
    assert "decommissioned" in exc.value.detail


async def test_a_collector_nobody_has_heard_of_may_still_ask(honoured):
    """Asking for work is how a collector registers at all."""
    token = security.mint_collector_token("col-new", get_settings())
    assert await _auth(token) == "col-new"


# --- sharding -----------------------------------------------------------------


def _eps(site: str, n: int, pinned: str | None = None) -> list[dict]:
    return [{"id": f"{site}-{i}", "site": site, "collector_id": pinned}
            for i in range(n)]


def test_placement_keeps_each_site_on_its_own_collector():
    """The bug a second collector found first: sites were never populated,
    so half of DC2 hashed to the machine in DC1."""
    collectors = [sharding.Collector("col-dc1", frozenset({"DC1"})),
                  sharding.Collector("col-dc2", frozenset({"DC2"}))]
    plan = sharding.plan(_eps("DC1", 50) + _eps("DC2", 50), collectors)
    assert all(owner == "col-dc1" for eid, owner in plan.items()
               if eid.startswith("DC1"))
    assert all(owner == "col-dc2" for eid, owner in plan.items()
               if eid.startswith("DC2"))


def test_a_draining_collector_gives_up_its_share_but_keeps_its_pins():
    collectors = [sharding.Collector("a", frozenset({"DC1"})),
                  sharding.Collector("b", frozenset({"DC1"}), accepting=False)]
    endpoints = [*_eps("DC1", 40),
                 {"id": "pinned", "site": "DC1", "collector_id": "b"}]
    plan = sharding.plan(endpoints, collectors)
    assert plan.pop("pinned") == "b"
    assert set(plan.values()) == {"a"}


def test_a_site_nobody_serves_is_unowned_not_handed_out():
    collectors = [sharding.Collector("col-dc1", frozenset({"DC1"}))]
    plan = sharding.plan(_eps("DC2", 5), collectors)
    assert set(plan.values()) == {None}


# --- platform checks ----------------------------------------------------------


def _signals(*collectors: p.Collector, expected_mapping_sha=None) -> p.Signals:
    return p.Signals(ingest_lag_s=0.4, telemetry_age_s=30.0,
                     telemetry_present=True, worker_heartbeat_age_s=5.0,
                     collectors=list(collectors), collectors_expected=len(collectors),
                     expected_mapping_sha=expected_mapping_sha)


def test_recent_drops_are_data_loss():
    findings = p.evaluate(_signals(p.Collector(
        "col-1", heartbeat_age_s=5.0, endpoints_owned=10, endpoints_online=10,
        publish_dropped=1200)))
    [f] = [f for f in findings if f.alarm_type == "collector_degraded"]
    assert f.severity == p.MAJOR and "1200 samples" in f.message


def test_a_filling_queue_warns_before_it_drops():
    findings = p.evaluate(_signals(p.Collector(
        "col-1", heartbeat_age_s=5.0, endpoints_owned=10, endpoints_online=10,
        publish_queue_depth=45_000, publish_queue_capacity=50_000)))
    assert [f.alarm_type for f in findings] == ["collector_degraded"]


def test_an_old_assignment_is_its_own_finding():
    findings = p.evaluate(_signals(p.Collector(
        "col-1", heartbeat_age_s=5.0, endpoints_owned=10, endpoints_online=10,
        assignment_age_s=900.0)))
    assert [f.alarm_type for f in findings] == ["assignment_stale"]


def test_a_collector_created_but_never_run_is_a_warning_not_an_outage():
    findings = p.evaluate(_signals(p.Collector(
        "col-dc2", heartbeat_age_s=None, endpoints_owned=0)))
    [f] = findings
    assert (f.alarm_type, f.severity) == ("collector_stale", p.WARNING)
    assert "never checked in" in f.message


def test_a_dead_collector_says_its_endpoints_went_unknown():
    findings = p.evaluate(_signals(p.Collector(
        "col-dc1", heartbeat_age_s=400.0, endpoints_owned=700)))
    [f] = findings
    assert f.severity == p.CRITICAL and "UNKNOWN" in f.message


# --- mapping bundle -------------------------------------------------------


def test_a_mapping_mismatch_is_a_warning_naming_both_shas():
    findings = p.evaluate(_signals(
        p.Collector("col-1", heartbeat_age_s=5.0, endpoints_owned=10,
                    mapping_bundle_sha="aaaa" * 16),
        expected_mapping_sha="bbbb" * 16))
    [f] = findings
    assert f.alarm_type == "mapping_mismatch" and f.severity == p.WARNING
    assert "aaaaaaaaaaaa" in f.message and "bbbbbbbbbbbb" in f.message


def test_a_matching_mapping_sha_raises_nothing():
    sha = "cccc" * 16
    findings = p.evaluate(_signals(
        p.Collector("col-1", heartbeat_age_s=5.0, endpoints_owned=10,
                    mapping_bundle_sha=sha),
        expected_mapping_sha=sha))
    assert findings == []


def test_an_older_collector_reporting_no_sha_is_silence_not_a_mismatch():
    findings = p.evaluate(_signals(
        p.Collector("col-1", heartbeat_age_s=5.0, endpoints_owned=10),
        expected_mapping_sha="bbbb" * 16))
    assert findings == []


def test_an_unreadable_platform_bundle_is_never_the_reason_to_alarm():
    """If the platform cannot compute its own digest, the comparison itself
    is untrustworthy - raising here would blame the collector for a fault
    entirely on this side."""
    findings = p.evaluate(_signals(
        p.Collector("col-1", heartbeat_age_s=5.0, endpoints_owned=10,
                    mapping_bundle_sha="aaaa" * 16),
        expected_mapping_sha=None))
    assert findings == []


# --- ingest ownership ---------------------------------------------------------


def _guard(plan: dict[str, str | None]) -> ownership.OwnershipGuard:
    g = ownership.OwnershipGuard()
    g._owner = dict(plan)
    g._owners = {o for o in plan.values() if o}
    g._refreshed = time.monotonic()
    return g


def test_only_the_owner_may_report_an_endpoint():
    g = _guard({"ep-1": "col-dc1"})
    assert g.allows("col-dc1", "ep-1")
    assert not g.allows("col-dc2", "ep-1")


def test_what_the_plan_cannot_judge_is_let_through():
    g = _guard({"ep-1": "col-dc1", "ep-2": None})
    assert g.allows("col-dc2", "ep-new")      # added since the last refresh
    assert g.allows("col-dc2", "ep-2")        # nobody can own it
    assert g.allows("", "ep-1")               # a collector older than the field


async def test_the_previous_owner_is_heard_out_after_a_move(monkeypatch):
    g = _guard({"ep-1": "col-a"})
    g._refreshed = time.monotonic() - 100  # loaded, and due

    async def moved(_session):
        return {"ep-1": "col-b"}

    monkeypatch.setattr(collector_service, "ownership", moved)
    await g.refresh(None)
    assert g.allows("col-b", "ep-1")
    assert g.allows("col-a", "ep-1")          # in flight when it moved
    g._previous["ep-1"] = ("col-a", time.monotonic() - 1)
    assert not g.allows("col-a", "ep-1")      # and not forever


def test_a_refusal_from_a_stranger_asks_for_an_early_refresh():
    g = _guard({"ep-1": "col-dc1"})
    assert not g.due()
    g.refuse("col-new", "telemetry.v1", 5)
    assert g.due()


# --- assignment ETag ----------------------------------------------------------


def _assignment(password: str) -> Assignment:
    # digest is what etag_for actually reads (docs/26 Phase 4) - build it the
    # same way app/services/collector.py's build_assignment does, so this
    # fixture exercises the real invariant rather than one only true because
    # both calls happened to leave the field at its default.
    payload = {"username": "root", "password": password}
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return Assignment(
        version=1, generated_at="2026-09-29T00:00:00Z", collector_id="col-1",
        endpoints=[AssignmentEndpoint(
            id="ep-1", device_id="d-1", device_name="SRV01", device_type="server",
            protocol="redfish", role="bmc", address="10.51.11.25",
            credential=AssignmentCredential(
                kind="redfish_basic", data=payload, digest=digest),
            poll=AssignmentPoll(interval_s=60, timeout_ms=8000, retries=1))])


def test_a_password_rotation_changes_the_etag():
    """It used to answer 304, and the collector kept the old password."""
    assert (collector_service.etag_for(_assignment("old"))
            != collector_service.etag_for(_assignment("new")))


def test_the_community_digest_is_what_the_collector_computes():
    import hashlib

    assert collector_service.community_digest({"community": "10.51.11.25"}) == (
        hashlib.sha256(b"10.51.11.25").hexdigest())
    assert collector_service.community_digest({"username": "x"}) is None
