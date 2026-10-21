"""The detector catalogue and the accumulator restore rule (docs/29 4.2, 10.4).

Pure: override merge, missing-point disable (ASHRAE G36), the severity cap,
and which accumulator a series resumes from after a restart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.analytics import catalogue as cat
from app.repositories import anomalies as repo

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


def _cr1(**kw) -> cat.Detector:
    base = {"id": "CR-1", "version": 1, "layer": 2, "phase": 1,
            "name": "CRAH supply over setpoint",
            "diagnosis": "supply air above setpoint while the valve is open",
            "device_types": ("crah",), "primary_metric": "supply_air_temp",
            "required_metrics": ("supply_air_temp", "valve_position_pct{CHW}"),
            "default_lane": "alert", "alarm_type": "crah_supply_over_setpoint",
            "severity": "MINOR",
            "params": {"v": 1, "dwell_s": 900, "margin_k": 3.0,
                       "gate": {"running_min_s": 600}}}
    base.update(kw)
    return cat.Detector(**base)


POINTS = [("supply_air_temp", ""), ("valve_position_pct", "CHW"), ("fan_speed_pct", "")]


def test_the_cap_is_minor():
    assert cat.DERIVED_SEVERITY_CAP == "MINOR"
    assert cat.cap_severity("CRITICAL") == "MINOR"
    assert cat.cap_severity("MAJOR") == "MINOR"
    assert cat.cap_severity("MINOR") == "MINOR"
    assert cat.cap_severity("WARNING") == "WARNING"
    # Unknown is capped, not passed through: the expensive mistake is a ticket.
    assert cat.cap_severity("BOGUS") == "MINOR"


def test_an_override_wins_key_by_key_and_replaces_nested_objects_whole():
    ov = cat.Override("CR-1", "dev-1", params={"margin_k": 4.0, "gate": {"x": 1}})
    eff = cat.effective(_cr1(), feature_enabled=True, device_type="crah", override=ov,
                        now=NOW, available=POINTS)
    assert eff.enabled
    assert eff.params["margin_k"] == 4.0
    assert eff.params["dwell_s"] == 900
    assert eff.params["gate"] == {"x": 1}  # jsonb || semantics


def test_a_mute_turns_it_off_until_it_expires():
    mute = cat.Override("CR-1", "dev-1", enabled=False, reason="sensor replaced",
                        expires_at=NOW + timedelta(hours=1))
    eff = cat.effective(_cr1(), feature_enabled=True, device_type="crah", override=mute,
                        now=NOW, available=POINTS)
    assert not eff.enabled and eff.reason == "muted"
    later = cat.effective(_cr1(), feature_enabled=True, device_type="crah", override=mute,
                          now=NOW + timedelta(hours=2), available=POINTS)
    assert later.enabled


def test_the_master_switch_and_the_row_switch_both_turn_it_off():
    assert cat.effective(_cr1(), feature_enabled=False, device_type="crah").reason == \
        "feature_off"
    assert cat.effective(_cr1(enabled=False), feature_enabled=True,
                         device_type="crah").reason == "detector_disabled"


def test_a_detector_is_off_on_a_device_missing_a_required_point():
    eff = cat.effective(_cr1(), feature_enabled=True, device_type="crah",
                        available=[("supply_air_temp", "")])
    assert not eff.enabled
    assert eff.reason == "missing_points"
    assert eff.missing == ("valve_position_pct{CHW}",)


def test_a_bare_required_key_is_met_by_any_instance():
    assert cat.missing_points(["valve_position_pct"], [("valve_position_pct", "CHW")]) == []
    assert cat.missing_points(["valve_position_pct{HW}"],
                              [("valve_position_pct", "CHW")]) == ["valve_position_pct{HW}"]
    with pytest.raises(ValueError):
        cat.parse_required("Bad Key")


def test_wrong_device_type_and_the_l5_switch_row_score_nothing():
    assert cat.effective(_cr1(), feature_enabled=True, device_type="pdu").reason == \
        "wrong_device_type"
    l5 = cat.Detector(id="L5", version=1, layer=5, phase=2, name="correlator",
                      diagnosis="switch", device_types=(), primary_metric="*",
                      params={"major_enabled": True})
    c = cat.Catalogue([_cr1(), l5])
    assert [d.id for d in c.scorers()] == ["CR-1"]
    assert c.l5_major_enabled() is True
    assert c.resolve("L5", "dev", feature_enabled=True, device_type=None).reason == "switch_row"
    assert cat.Catalogue([_cr1()]).l5_major_enabled() is False


def test_catalogue_resolves_through_the_device_override():
    c = cat.Catalogue([_cr1()], [cat.Override("CR-1", "dev-1", params={"margin_k": 5.0})])
    assert c.resolve("CR-1", "dev-1", feature_enabled=True,
                     device_type="crah").params["margin_k"] == 5.0
    assert c.resolve("CR-1", "dev-2", feature_enabled=True,
                     device_type="crah").params["margin_k"] == 3.0
    assert c.resolve("XX-1", "dev-1", feature_enabled=True, device_type="crah") is None


# --- restore (docs/29 10.4) --------------------------------------------------------

KEY = repo.SeriesKey("CH-2", "11111111-1111-1111-1111-111111111111", 42, "")


def _stored(updated_at, *, version=1, baseline="b1", last_ts="2026-10-21T08:55:00Z"):
    return repo.Checkpoint(key=KEY, detector_version=version, baseline_id=baseline,
                           state={"v": 1, "cusum_pos": 3.2, "last_ts": last_ts,
                                  "detector_version": version, "baseline_id": baseline},
                           updated_at=updated_at)


def test_the_acc_field_round_trips():
    assert repo.SeriesKey.from_acc_field(KEY.acc_field()) == KEY
    assert repo.SeriesKey.from_acc_field("DQ-1|d|7|PSU|1").instance == "PSU|1"


def test_a_recent_checkpoint_resumes():
    r = repo.restore(redis_state=None, stored=_stored(NOW - timedelta(minutes=5)),
                     detector_version=1, baseline_id="b1", now=NOW)
    assert r.source == "checkpoint" and r.state["cusum_pos"] == 3.2


@pytest.mark.parametrize(("kw", "reason"), [
    ({"updated_at": NOW - timedelta(hours=7)}, "restore_too_old"),
    ({"updated_at": NOW, "version": 2}, "detector_version"),
    ({"updated_at": NOW, "baseline": "b0"}, "baseline_changed"),
])
def test_a_checkpoint_is_reset_rather_than_resumed(kw, reason):
    updated = kw.pop("updated_at")
    r = repo.restore(redis_state=None, stored=_stored(updated, **kw),
                     detector_version=1, baseline_id="b1", now=NOW)
    assert r.source == "reset" and r.reset_reason == reason
    assert r.state["reset"] == {"reason": reason, "at": "2026-10-21T09:00:00Z"}
    assert "cusum_pos" not in r.state


def test_redis_wins_when_it_is_newer_and_loses_when_it_is_older():
    hot = {"v": 1, "cusum_pos": 9.9, "last_ts": "2026-10-21T08:59:00Z",
           "detector_version": 1, "baseline_id": "b1"}
    r = repo.restore(redis_state=hot, stored=_stored(NOW - timedelta(minutes=5)),
                     detector_version=1, baseline_id="b1", now=NOW)
    assert r.source == "redis" and r.state["cusum_pos"] == 9.9

    old = dict(hot, last_ts="2026-10-21T08:00:00Z")
    r = repo.restore(redis_state=old, stored=_stored(NOW - timedelta(minutes=5)),
                     detector_version=1, baseline_id="b1", now=NOW)
    assert r.source == "checkpoint"


def test_nothing_stored_starts_fresh():
    r = repo.restore(redis_state=None, stored=None, detector_version=3, baseline_id=None,
                     now=NOW)
    assert r.source == "fresh" and r.state == {"v": 1, "detector_version": 3,
                                               "baseline_id": None}


def test_the_fit_day_starts_at_the_configured_hour():
    assert repo.fit_day_start(NOW, 3) == datetime(2026, 10, 21, 3, tzinfo=UTC)
    assert repo.fit_day_start(NOW, 10) == datetime(2026, 10, 20, 10, tzinfo=UTC)
