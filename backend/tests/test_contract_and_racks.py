"""Contract codec, free-block computation and API smoke tests."""

from __future__ import annotations

import msgpack

from app.contracts.messages_gen import (
    SCHEMA_VERSION,
    Protocol,
    Quality,
    Telemetry,
    TelemetryBatch,
    ValueType,
    dt_to_ts,
    ts_to_dt,
)
from app.repositories.racks import compute_free_blocks


def _sample() -> Telemetry:
    return Telemetry(
        endpoint_id="e1", device_id="d1", metric="cpu_temperature",
        instance="cpu0", value_type=ValueType.GAUGE, double_value=67.5,
        unit="C", observed_at=1_755_512_400_000_000,
        collected_at=1_755_512_400_184_000,
        source_protocol=Protocol.REDFISH, quality=Quality.GOOD,
        metadata={"pointer": "/Thermal#/Temperatures/0"},
    )


def test_roundtrip_through_msgpack():
    batch = TelemetryBatch(collector_id="col-1", samples=[_sample()],
                           sent_at=1_755_512_400_200_000,
                           schema_version=SCHEMA_VERSION)
    blob = msgpack.packb(batch.to_dict(), use_bin_type=True)
    decoded = TelemetryBatch.from_dict(msgpack.unpackb(blob, raw=False))

    assert decoded.collector_id == "col-1"
    assert len(decoded.samples) == 1
    s = decoded.samples[0]
    assert s.metric == "cpu_temperature"
    assert s.double_value == 67.5
    assert s.metadata == {"pointer": "/Thermal#/Temperatures/0"}


def test_unknown_fields_are_ignored_forward_compatibility():
    """A newer collector adding a field must not break an older ingest."""
    d = TelemetryBatch(collector_id="c", samples=[_sample()]).to_dict()
    d["field_from_the_future"] = 42
    d["samples"][0]["another_new_one"] = "x"
    decoded = TelemetryBatch.from_dict(d)
    assert len(decoded.samples) == 1
    assert decoded.samples[0].metric == "cpu_temperature"


def test_timestamp_helpers_roundtrip():
    dt = ts_to_dt(1_755_512_400_000_000)
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt_to_ts(dt) == 1_755_512_400_000_000
    assert ts_to_dt(0) is None
    assert dt_to_ts(None) == 0


def test_free_blocks_in_an_empty_rack():
    blocks = compute_free_blocks(42, [])
    assert blocks == [{"u_start": 1, "u_height": 42}]


def test_free_blocks_are_ordered_largest_first():
    # U1 taken, U10-11 taken (a 2U device), U42 taken
    occupied = [(1, 1), (10, 2), (42, 1)]
    blocks = compute_free_blocks(42, occupied)
    assert blocks[0] == {"u_start": 12, "u_height": 30}
    assert {"u_start": 2, "u_height": 8} in blocks
    assert all(b["u_height"] > 0 for b in blocks)
    # Nothing overlaps a taken unit.
    covered = {u for b in blocks for u in range(b["u_start"], b["u_start"] + b["u_height"])}
    assert covered.isdisjoint({1, 10, 11, 42})


def test_free_blocks_handles_a_full_rack():
    occupied = [(u, 1) for u in range(1, 43)]
    assert compute_free_blocks(42, occupied) == []


def test_openapi_schema_builds():
    from app.main import app

    spec = app.openapi()
    assert "/api/v1/devices" in spec["paths"]
    assert "/api/v1/collector/assignments" in spec["paths"]
    assert "/api/v1/racks/{rack_id}/elevation" in spec["paths"]


def test_health_endpoint_needs_no_auth():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_instance_names_the_estate_and_needs_no_auth():
    """The login card renders before anybody has a token.

    It also must not leak the estate: an unauthenticated caller learns the
    organisation's name and the environment, which is what a browser tab needs,
    and nothing about sites, devices or alarms.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/v1/instance")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"org_name", "environment"}
        # Unset falls back to the product name rather than to an empty heading.
        assert body["org_name"]


def test_protected_endpoint_rejects_anonymous():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        assert client.get("/api/v1/devices").status_code == 401


def test_assignments_rejects_a_user_token_not_just_anonymous():
    """The assignments endpoint hands out decrypted credentials, so a browser
    token must not open it."""
    from fastapi.testclient import TestClient

    from app.core.security import issue_token
    from app.main import app

    token = issue_token("someone", "admin")
    with TestClient(app) as client:
        r = client.get("/api/v1/collector/assignments?collector_id=col-1",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401
