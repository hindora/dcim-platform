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


# --- Phase 1: the 0112 catalogue against the taxonomy, the registry and the
# --- param schema (docs/33 Phase 1 unit tests) --------------------------------

from app.core import alert_taxonomy as tax  # noqa: E402
from app.core.metrics_gen import METRICS  # noqa: E402
from tests.anomaly_fakes import seed_rows  # noqa: E402
from tests.test_alert_taxonomy import NON_TRAP_ALARM_TYPES  # noqa: E402

#: docs/29 section 9.3, verbatim.
CATEGORY_93 = {
    "crah_supply_over_setpoint": tax.COOLING, "crah_low_delta_t": tax.COOLING,
    "crah_valve_hunting": tax.COOLING, "chiller_chw_over_setpoint": tax.COOLING,
    "chiller_short_cycling": tax.COOLING, "chiller_condenser_rising": tax.COOLING,
    "chiller_low_delta_t": tax.COOLING, "cdu_facility_starved": tax.COOLING,
    "cdu_condensation_risk": tax.COOLING, "valve_position_mismatch": tax.COOLING,
    "valve_hunting": tax.COOLING, "pump_failed_to_prove": tax.COOLING,
    "tower_fan_saturated": tax.COOLING, "tower_basin_imbalance": tax.COOLING,
    "inlet_rate_of_change": tax.ENVIRONMENTAL, "rack_hot_spot": tax.ENVIRONMENTAL,
    "generator_fuel_loss": tax.POWER, "ats_transfer_rate": tax.POWER,
}
ROWS = seed_rows()


def test_0112_seeds_the_documented_rows():
    ids = {r["id"] for r in ROWS}
    assert {f"DQ-{n}" for n in range(1, 6)} <= ids and "L5" in ids
    l2 = [r for r in ROWS if r["layer"] == 2]
    assert len(l2) == 18
    assert {r["id"] for r in l2 if not r["enabled"]} == {"CT-2", "CT-3", "AT-1", "PL-1"}
    l5 = next(r for r in ROWS if r["id"] == "L5")
    assert l5["params"]["major_enabled"] is False and l5["layer"] == 5


def test_every_alarm_type_has_both_taxonomy_entries_and_its_93_category():
    typed = {r["alarm_type"]: r["id"] for r in ROWS if r["alarm_type"]}
    assert set(typed) == set(CATEGORY_93)
    for t in typed:
        assert tax.BY_ALARM_TYPE.get(t) == CATEGORY_93[t], t
        assert t in NON_TRAP_ALARM_TYPES, t


def test_alert_lane_rows_have_an_alarm_type_and_a_capped_severity():
    for r in ROWS:
        if r["default_lane"] == "alert":
            assert r["alarm_type"] and r["severity"] in ("MINOR", "WARNING"), r["id"]
    assert len({r["alarm_type"] for r in ROWS if r["alarm_type"]}) == \
        sum(1 for r in ROWS if r["alarm_type"])


def test_required_metrics_are_registry_keys():
    for r in ROWS:
        for spec in r["required_metrics"]:
            key, _ = cat.parse_required(spec)
            assert key in METRICS, (r["id"], spec)
        if r["primary_metric"] != "*":
            assert r["primary_metric"] in METRICS, r["id"]
        for t in r["device_types"]:
            assert t in cat.L0_METRICS or t == "ats", (r["id"], t)


@pytest.mark.parametrize("row", ROWS, ids=[r["id"] for r in ROWS])
def test_param_schema_defaults_are_the_seed_values(row):
    specs = cat.PARAM_SCHEMAS[row["id"]]
    assert specs, row["id"]
    for spec in specs:
        assert spec.key in row["params"], (row["id"], spec.key)
        assert row["params"][spec.key] == spec.default, (row["id"], spec.key)
        # and the seed is inside its own bounds
        assert cat.validate_params(row["id"], {spec.key: spec.default})


def test_validate_params_refuses_unknown_and_out_of_range():
    with pytest.raises(cat.ParamError) as e:
        cat.validate_params("CR-1", {"bogus": 1})
    assert e.value.error == "unknown_param" and e.value.key == "bogus"
    with pytest.raises(cat.ParamError) as e:
        cat.validate_params("CR-1", {"margin_k": 50})
    assert e.value.error == "param_out_of_range"
    with pytest.raises(cat.ParamError):
        cat.validate_params("CR-1", {"dwell_s": 90.5})
    with pytest.raises(cat.ParamError):
        cat.validate_params("CR-1", {"margin_k": True})
    with pytest.raises(cat.ParamError):
        cat.validate_params("L5", {"major_enabled": "yes"})
    assert cat.validate_params("CR-1", {"dwell_s": 600.0}) == {"dwell_s": 600}
    assert cat.validate_params("L5", {"major_enabled": True}) == {"major_enabled": True}
