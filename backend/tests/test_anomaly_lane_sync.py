"""A detector's lane decides, at every sweep, whether its open findings carry alarms.

Moved to the panel (advisory): the derived alarm clears with a history entry and
the finding stays open. Moved to the alert lane: the alarm is raised on the next
sweep that still sees the condition.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.analytics import sweep as sw


class _Cat:
    def __init__(self, lanes):
        self.lanes = lanes

    def get(self, did):
        lane = self.lanes.get(did)
        if lane is None:
            return None
        return SimpleNamespace(id=did, layer=2, default_lane=lane, alarm_type=f"t_{did}")


def _engine(monkeypatch, lanes, rows):
    log = {"lane": [], "cleared": [], "history": []}

    async def set_lane(session, fid, lane):
        log["lane"].append((fid, lane))

    async def clear(session, *, device_id, alarm_types, instance, at, by):
        log["cleared"].append((device_id, alarm_types[0]))
        return [{"id": "al1", "severity": "MINOR", "device_id": device_id}]

    async def history(session, **kw):
        log["history"].append(kw)

    monkeypatch.setattr(sw.repo, "set_finding_lane", set_lane)
    monkeypatch.setattr(sw.alarm_repo, "clear_alarms", clear)
    monkeypatch.setattr(sw.alarm_repo, "record_history", history)
    eng = object.__new__(sw._Engine)
    eng.s, eng.cat, eng.now = None, _Cat(lanes), datetime.now(UTC)
    eng.result = sw.SweepResult(ran=True)
    eng.touched, eng.frames, eng.open_rows = set(), {}, rows
    return eng, log


def _row(det, lane, alarm_id="al1"):
    return {"id": f"f-{det}", "detector_id": det, "device_id": "d1", "instance": "",
            "lane": lane, "alarm_id": alarm_id}


async def test_moving_to_the_panel_clears_the_alarm_and_keeps_the_finding(monkeypatch):
    rows = [_row("CR-2", "alert")]
    eng, log = _engine(monkeypatch, {"CR-2": "advisory"}, rows)
    await eng._sync_lanes()
    assert log["lane"] == [("f-CR-2", "advisory")]
    assert log["cleared"] == [("d1", "t_CR-2")]
    assert log["history"][0]["detail"]["resolution"] == "advisory_only"
    assert [a.kind for a in eng.result.actions] == ["alarm_cleared"]
    assert eng.touched == {"d1"} and rows[0]["lane"] == "advisory"


async def test_moving_to_the_alert_lane_marks_the_finding_for_its_alarm(monkeypatch):
    rows = [_row("PU-3", "advisory", alarm_id=None)]
    eng, log = _engine(monkeypatch, {"PU-3": "alert"}, rows)
    await eng._sync_lanes()
    assert log["lane"] == [("f-PU-3", "alert")] and log["cleared"] == []
    assert rows[0]["promote"] is True


async def test_a_finding_already_in_its_lane_is_left_alone(monkeypatch):
    rows = [_row("CR-1", "advisory", alarm_id=None)]
    eng, log = _engine(monkeypatch, {"CR-1": "advisory"}, rows)
    await eng._sync_lanes()
    assert log == {"lane": [], "cleared": [], "history": []}
