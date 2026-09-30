"""The collector's capacity report on its way in (docs/26 Phase 5): decoded
once at storage, read back by the monitor. Anything missing or malformed is
"not reported" - never a zero load, which would read as a healthy collector."""

from __future__ import annotations

from types import SimpleNamespace

from app.alarms.platform_monitor import _capacity_fields
from app.ingest.worker import _capacity
from app.services.collector_capacity import decode


def hb(raw: str):
    return SimpleNamespace(collector_id="col-1", capacity=raw)


def test_a_report_is_stored_as_an_object():
    assert _capacity(hb('{"busy_pct": 42.0, "window_s": 300}')) == {
        "busy_pct": 42.0, "window_s": 300}


def test_absent_or_unreadable_is_none_not_empty():
    assert _capacity(hb("")) is None
    assert _capacity(hb("{not json")) is None
    assert _capacity(hb("[1, 2]")) is None
    assert _capacity(SimpleNamespace(collector_id="old")) is None


def test_fields_read_back_for_the_rules():
    assert _capacity_fields({"busy_pct": 88.2, "window_s": 300, "shed": 1, "late": 4}) == {
        "capacity_busy_pct": 88.2, "capacity_window_s": 300.0,
        "capacity_shed": 1, "capacity_late": 4}


def test_no_report_gives_no_fields_so_the_rule_stays_silent():
    assert _capacity_fields(None) == {}
    assert _capacity_fields({}) == {}
    assert _capacity_fields({"busy_pct": None}) == {}
    assert _capacity_fields({"busy_pct": "high"}) == {}


def test_the_http_fallback_decodes_through_the_same_function():
    """A gateway-transport collector POSTs the flat heartbeat; its capacity
    string must land as the same object the Redis path stores."""
    assert decode('{"busy_pct": 5}', "col-1") == {"busy_pct": 5}
    assert decode(None) is None and decode(42) is None
