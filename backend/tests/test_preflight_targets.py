"""Preflight reachability targets (docs/26 Phase 8) - pure sampling."""

from __future__ import annotations

from app.services.collector import sample_targets


def _row(proto, addr, port=None):
    return {"protocol": proto, "address": addr, "port": port}


def test_groups_by_protocol_and_fills_the_default_port():
    out = sample_targets([_row("modbus", "10.52.1.20"), _row("redfish", "10.51.1.5", 8443)])
    assert out == [
        {"protocol": "modbus", "targets": [{"address": "10.52.1.20", "port": 502}], "total": 1},
        {"protocol": "redfish", "targets": [{"address": "10.51.1.5", "port": 8443}], "total": 1},
    ]


def test_field_devices_sharing_a_gateway_address_are_one_target():
    rows = [_row("modbus", "10.52.1.20") for _ in range(18)]
    out = sample_targets(rows)
    assert out[0]["targets"] == [{"address": "10.52.1.20", "port": 502}]
    assert out[0]["total"] == 1


def test_caps_per_protocol_and_reports_the_total():
    rows = [_row("snmp", f"10.51.1.{i}") for i in range(1, 11)]
    out = sample_targets(rows, per_protocol=3)
    assert len(out[0]["targets"]) == 3 and out[0]["total"] == 10


def test_sample_is_deterministic_across_input_order():
    rows = [_row("snmp", f"10.51.1.{i}") for i in range(1, 11)]
    assert sample_targets(rows) == sample_targets(list(reversed(rows)))


def test_inbound_only_protocols_and_addressless_rows_are_dropped():
    out = sample_targets([_row("snmp_trap", "10.51.1.1"), _row("sflow", "10.51.1.2"),
                          _row("bacnet", None)])
    assert out == []


def test_a_protocol_with_no_known_port_is_dropped_not_guessed():
    assert sample_targets([_row("manual", "10.51.1.1")]) == []


# ------------------------------------------------ collector detail summary

from app.services.collector import summarise_detail  # noqa: E402


def _ep(status, proto="snmp", err=None, failure=None, reported_by="col-1"):
    return {"status": status, "protocol": proto, "last_error": err,
            "last_failure": failure, "reported_by": reported_by}


def test_summary_counts_by_status_and_protocol():
    out = summarise_detail("col-1", [_ep("ONLINE"), _ep("ONLINE", "bacnet"),
                                     _ep("OFFLINE", "bacnet")], {})
    assert out["by_status"] == {"OFFLINE": 1, "ONLINE": 2}
    assert out["by_protocol"] == {"bacnet": 2, "snmp": 1}


def test_recent_errors_newest_first_and_capped():
    eps = [_ep("DEGRADED", err=f"e{i}", failure=f"2026-09-30T10:0{i}:00") for i in range(5)]
    out = summarise_detail("col-1", eps, {}, recent=3)
    assert [e["last_error"] for e in out["recent_errors"]] == ["e4", "e3", "e2"]
    assert out["error_count"] == 5


def test_reported_elsewhere_counts_endpoints_another_collector_last_reported():
    out = summarise_detail("col-1", [_ep("ONLINE"), _ep("ONLINE", reported_by="col-2"),
                                     _ep("UNKNOWN", reported_by=None)], {})
    assert out["reported_elsewhere"] == 1


def test_missing_stats_stay_none_not_zero():
    out = summarise_detail("col-1", [], {"queue_depth": 0, "polls_total": 10})
    assert out["stats"]["spool_bytes"] is None
    assert out["stats"]["queue_depth"] == 0
    assert out["queue_fill_pct"] is None, "no capacity reported: no percentage invented"


def test_queue_fill_is_depth_over_capacity():
    out = summarise_detail("col-1", [], {"queue_depth": 250, "queue_capacity": 1000})
    assert out["queue_fill_pct"] == 25.0
