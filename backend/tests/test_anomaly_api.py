"""The anomaly API over HTTP (docs/30), with the repository faked.

Feedback: all 20 cells of the section 4.3 state machine, a required dismiss
reason, the 409 body naming what IS allowed, and the one acknowledgement
path to the alarm. PATCH validation against param_schema. 403 for a
hand-minted viewer token on every mutation (POST /login only issues admin,
api/v1/misc.py). Verdict surviving resolution is a database property and is
checked in tests/test_anomaly_rules_live.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.analytics import sweep as anomaly_sweep
from app.api.v1 import anomalies as api
from app.core.config import get_settings
from app.core.security import issue_token
from app.db.session import get_session
from app.main import create_app
from tests.anomaly_fakes import FakeRedis, seed_rows

P = "/api/v1/anomalies"
FID = "6c1f6e1e-6a51-4c0b-9b55-0f2d7b1d2a11"
DEV = "1b0e6e1e-6a51-4c0b-9b55-0f2d7b1d2a11"


def _tok(role: str) -> dict:
    return {"Authorization": f"Bearer {issue_token('u-' + role, role)}"}


class _Session:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None

    async def execute(self, *a, **kw):
        raise RuntimeError("no database in this test")

    async def scalar(self, *a, **kw):
        return None


class _Repo:
    """The finding / detector state the endpoints read and write."""

    def __init__(self):
        self.finding = {"id": FID, "state": "open", "lane": "advisory", "kind": "process",
                        "alarm_id": None, "device_id": DEV, "detector_id": "CR-1",
                        "first_seen": datetime(2026, 10, 21, tzinfo=UTC), "verdict": None}
        self.feedback: list[dict] = []
        self.detectors = [{**r, "version": 1, "cadence_s": 60, "updated_at": None,
                           "updated_by": None} for r in seed_rows()]
        self.updated: list[dict] = []
        self.audit: list[dict] = []
        self.acked: list[str] = []

    async def get_finding(self, session, fid):
        return dict(self.finding) if fid == FID else None

    async def finding_state(self, session, fid):
        return self.finding["state"] if fid == FID else None

    async def apply_feedback(self, session, fid, *, action, actor, reason, note,
                             via="finding"):
        frm, to = api.repo.TRANSITIONS[action]
        if self.finding["state"] not in frm:
            return None
        old = self.finding["state"]
        self.finding["state"] = to
        self.feedback.append({"action": action, "reason": reason, "from": old, "to": to})
        return {"from_state": old, "to_state": to, **{k: self.finding[k] for k in (
            "kind", "lane", "alarm_id", "device_id", "first_seen", "detector_id")}}

    async def findings_by_ids(self, session, ids):
        return [{**self.finding, "verdict": None}]

    async def detector_rows(self, session):
        return [dict(r) for r in self.detectors]

    async def detector_row(self, session, did):
        return next((dict(r) for r in self.detectors if r["id"] == did), None)

    async def device_points(self, session, types):
        return {DEV: ("crah", [("supply_air_temp", ""), ("air_setpoint_temp", ""),
                               ("valve_position_pct", "CHW"),
                               ("equipment_state", "Unit_Running")]),
                "other": ("crah", [("supply_air_temp", "")])}

    async def detector_stats(self, session):
        return {"CR-1": {"open_findings": 1, "confirmed": 3, "dismissed": 1}}

    async def update_detector(self, session, did, *, enabled, params, bump, actor):
        self.updated.append({"id": did, "enabled": enabled, "params": params, "bump": bump})
        return {"id": did, "version": 2 if bump else 1}

    async def open_findings_for(self, session, did, dev=None):
        return []

    async def device_exists(self, session, did):
        return did == DEV

    async def get_override(self, session, det, dev):
        return None

    async def upsert_override(self, session, det, dev, **kw):
        self.updated.append({"override": (det, dev), **kw})

    async def delete_override(self, session, det, dev):
        return False

    async def get_exclusion(self, session, xid):
        return {"id": xid, "source": "confirmed_finding"}


@pytest.fixture
def client(monkeypatch):
    fake = _Repo()
    for name in ("get_finding", "finding_state", "apply_feedback", "findings_by_ids",
                 "detector_rows", "detector_row", "device_points", "detector_stats",
                 "update_detector", "open_findings_for", "device_exists", "get_override",
                 "upsert_override", "delete_override", "get_exclusion"):
        monkeypatch.setattr(api.repo, name, getattr(fake, name))

    async def no_frames(session, changes, cap=50):
        return [{"event": e, "change": c, "via": v} for _, (e, c, v) in changes.items()]

    async def record(session, **kw):
        fake.audit.append(kw)

    async def ack(session, alarm_id, actor, note):
        fake.acked.append(alarm_id)
        return {"id": alarm_id, "device_id": DEV, "alarm_type": "x", "severity": "MINOR"}

    async def history(session, **kw):
        return None

    async def get_alarm(session, alarm_id):
        return {"id": alarm_id, "device_id": DEV, "severity": "MINOR"}

    monkeypatch.setattr(anomaly_sweep, "build_finding_frames", no_frames)
    monkeypatch.setattr(api.audit, "record", record)
    monkeypatch.setattr(api.alarm_repo, "acknowledge", ack)
    monkeypatch.setattr(api.alarm_repo, "record_history", history)
    monkeypatch.setattr(api.alarm_repo, "get_alarm", get_alarm)

    app = create_app()
    settings = get_settings().model_copy(update={"anomaly_enabled": True})
    session, redis = _Session(), FakeRedis()

    async def s():
        yield session

    async def r():
        yield redis

    app.dependency_overrides[get_session] = s
    app.dependency_overrides[api.get_redis] = r
    app.dependency_overrides[get_settings] = lambda: settings
    tc = TestClient(app, raise_server_exceptions=False)
    tc.fake, tc.redis, tc.session = fake, redis, session
    return tc


# --- feedback: the state machine ------------------------------------------------

ACTIONS = ("acknowledge", "dismiss", "confirm", "reopen")
TABLE = {  # docs/30 section 4.3, row by row
    "open": {"acknowledge": "acknowledged", "dismiss": "dismissed", "confirm": "confirmed"},
    "acknowledged": {"dismiss": "dismissed", "confirm": "confirmed", "reopen": "open"},
    "confirmed": {"reopen": "open"},
    "dismissed": {"reopen": "open"},
    "resolved": {},
}


@pytest.mark.parametrize("state", list(TABLE))
@pytest.mark.parametrize("action", ACTIONS)
def test_every_cell_of_the_state_machine(client, state, action):
    client.fake.finding["state"] = state
    body = {"action": action}
    if action == "dismiss":
        body["reason"] = "not_a_fault"
    r = client.post(f"{P}/findings/{FID}/feedback", json=body, headers=_tok("operator"))
    want = TABLE[state].get(action)
    if want:
        assert r.status_code == 200, r.text
        assert client.fake.finding["state"] == want
        assert client.session.commits == 1
        assert [a["action"] for a in client.fake.audit] == ["anomaly.finding.feedback"]
    else:
        assert r.status_code == 409, r.text
        d = r.json()["detail"]
        assert d["error"] == "illegal_transition" and d["state"] == state
        assert d["allowed"] == list(TABLE[state])
        assert client.session.commits == 0


def test_acknowledge_twice_names_what_is_allowed(client):
    h = _tok("operator")
    assert client.post(f"{P}/findings/{FID}/feedback", json={"action": "acknowledge"},
                       headers=h).status_code == 200
    r = client.post(f"{P}/findings/{FID}/feedback", json={"action": "acknowledge"}, headers=h)
    assert r.status_code == 409
    assert r.json()["detail"]["allowed"] == ["dismiss", "confirm", "reopen"]


def test_dismiss_needs_a_reason_and_only_dismiss_takes_one(client):
    h = _tok("operator")
    r = client.post(f"{P}/findings/{FID}/feedback", json={"action": "dismiss"}, headers=h)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "reason_required"
    r = client.post(f"{P}/findings/{FID}/feedback",
                    json={"action": "confirm", "reason": "duplicate"}, headers=h)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "reason_not_allowed"
    r = client.post(f"{P}/findings/{FID}/feedback",
                    json={"action": "dismiss", "reason": "because"}, headers=h)
    assert r.status_code == 422                      # not a DismissReason
    assert client.fake.finding["state"] == "open"


def test_reopen_then_dismiss_is_two_audited_steps(client):
    h = _tok("operator")
    client.fake.finding["state"] = "confirmed"
    assert client.post(f"{P}/findings/{FID}/feedback", json={"action": "reopen"},
                       headers=h).status_code == 200
    assert client.post(f"{P}/findings/{FID}/feedback",
                       json={"action": "dismiss", "reason": "sensor_fault"},
                       headers=h).status_code == 200
    assert [f["action"] for f in client.fake.feedback] == ["reopen", "dismiss"]
    assert len(client.fake.audit) == 2


def test_acknowledging_an_alert_lane_finding_acknowledges_its_alarm(client):
    client.fake.finding.update(lane="alert", alarm_id="a1")
    r = client.post(f"{P}/findings/{FID}/feedback", json={"action": "acknowledge"},
                    headers=_tok("operator"))
    assert r.status_code == 200
    assert client.fake.acked == ["a1"]
    assert {a["action"] for a in client.fake.audit} == {"alarm.acknowledge",
                                                        "anomaly.finding.feedback"}
    events = [b["event"] for c, b in client.redis.published]
    assert "alarm_updated" in events and "anomaly.finding.updated" in events


def test_dismissing_an_alert_lane_finding_leaves_the_alarm_alone(client):
    client.fake.finding.update(lane="alert", alarm_id="a1")
    client.post(f"{P}/findings/{FID}/feedback",
                json={"action": "dismiss", "reason": "expected_operation"},
                headers=_tok("operator"))
    assert client.fake.acked == []


def test_an_unknown_finding_is_404(client):
    r = client.post(f"{P}/findings/00000000-0000-0000-0000-000000000000/feedback",
                    json={"action": "acknowledge"}, headers=_tok("operator"))
    assert r.status_code == 404 and r.json()["detail"]["error"] == "not_found"


# --- the list ---------------------------------------------------------------------

def test_an_unknown_enum_is_400(client):
    r = client.get(f"{P}/findings?lane=bogus", headers=_tok("viewer"))
    assert r.status_code == 400 and r.json()["detail"]["error"] == "unknown_enum"


def test_feature_off_returns_empty_pages(client, monkeypatch):
    settings = get_settings().model_copy(update={"anomaly_enabled": False})
    client.app.dependency_overrides[get_settings] = lambda: settings
    r = client.get(f"{P}/findings?with_total=true&with_facets=true", headers=_tok("viewer"))
    assert r.status_code == 200
    body = r.json()
    assert sorted(body) == ["facets", "items", "next_cursor", "total"]
    assert body["items"] == [] and body["total"] == 0


# --- detectors -----------------------------------------------------------------------

def test_the_catalogue_carries_param_schema_and_counters(client):
    r = client.get(f"{P}/detectors", headers=_tok("viewer"))
    assert r.status_code == 200
    items = {d["id"]: d for d in r.json()["items"]}
    cr1 = items["CR-1"]
    assert cr1["layer"] == "L2" and cr1["severity"] == "MINOR"
    keys = {s["key"]: s for s in cr1["param_schema"]}
    assert keys["margin_k"]["default"] == 3.0 and keys["margin_k"]["min"] == 0.5
    assert cr1["devices_in_scope"] == 1 and cr1["inactive_devices"] == 1
    assert cr1["precision"] == 0.75
    assert items["L5"]["layer"] == "L5" and items["L5"]["lane"] is None
    assert items["L5"]["params"]["major_enabled"] is False


@pytest.mark.parametrize("params,error", [({"margin_k": 50}, "param_out_of_range"),
                                          ({"margin_k": "x"}, "param_out_of_range"),
                                          ({"nope": 1}, "unknown_param")])
def test_patch_validates_against_param_schema(client, params, error):
    r = client.patch(f"{P}/detectors/CR-1", json={"params": params}, headers=_tok("admin"))
    assert r.status_code == 422 and r.json()["detail"]["error"] == error
    assert client.fake.updated == []


def test_patch_bumps_the_version_on_params_but_not_on_enabled_or_l5(client):
    h = _tok("admin")
    r = client.patch(f"{P}/detectors/CR-1", json={"params": {"margin_k": 3.5}}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == ["params.margin_k"] and client.fake.updated[-1]["bump"]
    r = client.patch(f"{P}/detectors/CR-1", json={"enabled": False}, headers=h)
    assert r.json()["changed"] == ["enabled"] and not client.fake.updated[-1]["bump"]
    r = client.patch(f"{P}/detectors/L5", json={"params": {"major_enabled": True}}, headers=h)
    assert r.status_code == 200 and not client.fake.updated[-1]["bump"]
    r = client.patch(f"{P}/detectors/CR-1", json={"params": {"margin_k": 3.0}}, headers=h)
    assert r.json()["changed"] == []                  # a no-op
    assert client.redis.kv[api.CATALOGUE_VERSION_KEY] == "3"
    r = client.patch(f"{P}/detectors/XX-1", json={"enabled": True}, headers=h)
    assert r.status_code == 404 and r.json()["detail"]["error"] == "unknown_detector"
    r = client.patch(f"{P}/detectors/CR-1", json={"bogus": True}, headers=h)
    assert r.status_code == 422                       # extra fields are forbidden


# --- overrides and exclusions ---------------------------------------------------------

def test_override_validation(client):
    h = _tok("operator")
    url = f"{P}/overrides/CR-1/{DEV}"
    r = client.put(url, json={"enabled": False}, headers=h)
    assert r.json()["detail"]["error"] == "mute_needs_reason"
    r = client.put(url, json={}, headers=h)
    assert r.json()["detail"]["error"] == "override_noop"
    r = client.put(url, json={"params": {"margin_k": 99}}, headers=h)
    assert r.json()["detail"]["error"] == "param_out_of_range"
    r = client.put(f"{P}/overrides/CR-1/00000000-0000-0000-0000-000000000000",
                   json={"enabled": False, "reason": "x"}, headers=h)
    assert r.status_code == 404 and r.json()["detail"]["error"] == "not_found"
    r = client.put(f"{P}/overrides/ZZ-1/{DEV}", json={"enabled": False, "reason": "x"},
                   headers=h)
    assert r.json()["detail"]["error"] == "unknown_detector"


def test_exclusion_validation(client):
    h = _tok("operator")
    base = {"starts_at": "2026-10-18T06:00:00+00:00", "reason": "r"}
    r = client.post(f"{P}/exclusions", json=base, headers=h)
    assert r.json()["detail"]["error"] == "exclusion_scope"
    r = client.post(f"{P}/exclusions", json={**base, "device_id": DEV,
                                             "ends_at": "2026-10-18T05:00:00+00:00"},
                    headers=h)
    assert r.json()["detail"]["error"] == "exclusion_dates_invalid"
    r = client.post(f"{P}/exclusions", json={**base, "device_id": DEV,
                                             "source": "maintenance"}, headers=h)
    assert r.json()["detail"]["error"] == "source_not_allowed"
    r = client.delete(f"{P}/exclusions/{FID}", headers=h)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "exclusion_system_owned"


# --- authorisation ----------------------------------------------------------------------

MUTATIONS = [
    ("post", f"{P}/findings/{FID}/feedback", {"action": "acknowledge"}),
    ("patch", f"{P}/detectors/CR-1", {"enabled": False}),
    ("put", f"{P}/overrides/CR-1/{DEV}", {"enabled": False, "reason": "x"}),
    ("delete", f"{P}/overrides/CR-1/{DEV}", None),
    ("post", f"{P}/exclusions", {"device_id": DEV, "starts_at": "2026-10-18T06:00:00Z",
                                 "reason": "r"}),
    ("delete", f"{P}/exclusions/{FID}", None),
]


@pytest.mark.parametrize("method,url,body", MUTATIONS, ids=[f"{m} {u}" for m, u, _ in MUTATIONS])
def test_a_viewer_cannot_mutate_anything(client, method, url, body):
    kw = {"headers": _tok("viewer")}
    if body is not None:
        kw["json"] = body
    r = getattr(client, method)(url, **kw)
    assert r.status_code == 403, r.text
    assert client.session.commits == 0 and client.fake.updated == []


def test_only_an_admin_patches_a_detector(client):
    r = client.patch(f"{P}/detectors/L5", json={"params": {"major_enabled": True}},
                     headers=_tok("operator"))
    assert r.status_code == 403


@pytest.mark.parametrize("url", [f"{P}/findings", f"{P}/detectors", f"{P}/overrides",
                                 f"{P}/exclusions", f"{P}/health"])
def test_a_viewer_reads_everything(client, monkeypatch, url):
    async def empty(session, **kw):
        return {"items": [], "next_cursor": None, "total": None}

    async def counts(session):
        return {"findings": {"open": 0, "opened_24h": 0,
                             "by_lane": {"advisory": 0, "alert": 0},
                             "by_kind": {"sensor": 0, "process": 0, "undetermined": 0}},
                "findings_per_hour": 0, "incidents": {"open": 0},
                "baseline_last_fit_at": None, "l5_major_enabled": False}

    for name in ("list_findings", "list_overrides", "list_exclusions"):
        monkeypatch.setattr(api.repo, name, empty)
    monkeypatch.setattr(api.repo, "health_counts", counts)
    r = client.get(url, headers=_tok("viewer"))
    assert r.status_code == 200, r.text
    if url.endswith("/health"):
        assert r.json()["severity_cap"] == "MINOR" and r.json()["l5_major_enabled"] is False
