"""120 openings in one sweep -> at most 50 finding frames + 1 summary.

The websocket hub queues 256 frames per session and closes the session when
the queue fills (websocket/hub.py), so the sweep caps what it publishes on
`anomalies` (docs/30 section 8, docs/33 task 1.11).
"""

from __future__ import annotations

from app.analytics import sweep as anomaly_sweep
from app.ingest.fanout import Fanout
from tests.anomaly_fakes import FakeDB, FakeRedis, sweep_once

T0 = 1_800_000_000.0


async def test_build_caps_at_50_plus_one_bulk(monkeypatch):
    ids = [f"f{i:03d}" for i in range(120)]

    async def rows(session, keep):
        return [{"id": i, "device_id": f"d{i}"} for i in keep]

    monkeypatch.setattr(anomaly_sweep.repo, "findings_by_ids", rows)
    changes = dict.fromkeys(ids, ("anomaly.finding.opened", None, None))
    frames = await anomaly_sweep.build_finding_frames(object(), changes)
    assert len(frames) == 51
    assert all(f["event"] == "anomaly.finding.opened" for f in frames[:50])
    assert frames[-1] == {"event": "anomaly.finding.updated", "change": "bulk", "count": 70}


async def test_one_frame_per_finding_and_openings_first(monkeypatch):
    async def rows(session, keep):
        return [{"id": i, "device_id": "d"} for i in keep]

    monkeypatch.setattr(anomaly_sweep.repo, "findings_by_ids", rows)
    changes = {"b": ("anomaly.finding.updated", "acknowledged", "alarm"),
               "a": ("anomaly.finding.opened", None, None)}
    frames = await anomaly_sweep.build_finding_frames(object(), changes, cap=1)
    assert [f["event"] for f in frames] == ["anomaly.finding.opened",
                                             "anomaly.finding.updated"]
    assert frames[1] == {"event": "anomaly.finding.updated", "change": "bulk", "count": 1}
    assert await anomaly_sweep.build_finding_frames(object(), {}) == []


async def test_a_storm_through_the_sweep_and_the_fanout(monkeypatch):
    db = FakeDB()
    db.patch(monkeypatch)
    for n in range(120):
        dev = db.add_device("sensor", f"SEN-{n}", poll_interval_s=60)
        for k in range(0, 1200, 60):
            db.sample(dev, "inlet_temperature", T0 - 1200 + k, 22.0)
        # ...and then nothing for 10 min while another series still reports.
        for k in range(0, 1800, 60):
            db.sample(dev, "relative_humidity", T0 - 1200 + k, 45.0 + (k % 120) / 60)
    redis = FakeRedis()
    r = await sweep_once(db, T0 + 600, redis)
    assert r.findings_opened == 120               # DQ-1 on every probe at once
    finding_frames = [f for f in r.frames if "finding" in f]
    assert len(finding_frames) == 50 and r.frames[-1]["change"] == "bulk"
    assert r.frames[-1]["count"] == 70

    await Fanout(redis).anomaly_frames(r.frames)
    on_topic = [b for c, b in redis.published if c == "dcim:ws:anomalies"]
    assert len(on_topic) == 51
    on_device = [c for c, b in redis.published if c.startswith("dcim:ws:device:")]
    assert len(on_device) == 50

    # A second sweep with nothing new publishes no "still true" frames.
    r = await sweep_once(db, T0 + 660, redis)
    assert r.frames == []
