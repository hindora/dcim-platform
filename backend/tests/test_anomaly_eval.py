"""The scoring harness (docs/33 section 4) against fixtures.

Matching, the poll-interval tolerance across repeated runs, detection delay,
lead over L1, false findings per device-day, and compression. Plus the CLI's
behaviour when the simulator's campaigns file does not exist yet.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.analytics import evaluation as ev

T0 = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
CRAH = "43ab2687"
OTHER = "903e3e7f"

SCENARIO = ev.Scenario.from_json({
    "name": "crah-supply-bias", "tier": "sensor", "runs": 3,
    "expected": [{"detector": "CR-5", "kind": "sensor", "root": "injected_device",
                  "window_s": {"min": 60, "max": 900, "calibrated": False}}],
    "expected_misses": [], "must_not": ["CR-1"], "enable_for_run": [],
})


def _ledger(run: str, start: datetime, device=CRAH, **kw) -> dict:
    return {"id": f"flt_{run}", "kind": "sensor", "device_id": device,
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": (start + timedelta(hours=1)).isoformat(),
            "expected_kind": "sensor", "campaign": f"crah-supply-bias/{run}", **kw}


def _det(i, detector="CR-5", at=T0, device=CRAH, kind="sensor", **kw) -> ev.Detection:
    return ev.Detection(id=str(i), detector_id=detector, device_id=device,
                        opened_at=at, kind=kind, **kw)


def test_parse_ledger_joins_runs_to_scenarios():
    inj = ev.parse_ledger({"entries": [_ledger("r2", T0 + timedelta(hours=2)),
                                       _ledger("r1", T0)]})
    assert [i.run_id for i in inj] == ["r1", "r2"]
    assert inj[0].scenario == "crah-supply-bias"
    assert inj[0].start == T0


def test_a_detection_matches_inside_the_window_on_the_root():
    inj = ev.parse_ledger([_ledger("r1", T0)])[0]
    m = ev.match_injection(inj, [_det(1, at=T0 + timedelta(seconds=300))], SCENARIO)
    assert m.detected and m.delay_s == 300 and m.detector_id == "CR-5"


@pytest.mark.parametrize(("det", "why"), [
    (_det(1, at=T0 + timedelta(seconds=30)), "too early"),
    (_det(1, at=T0 + timedelta(seconds=2000)), "too late"),
    (_det(1, device=OTHER, at=T0 + timedelta(seconds=300)), "wrong root"),
    (_det(1, detector="CR-1", at=T0 + timedelta(seconds=300)), "not expected"),
])
def test_a_detection_that_breaks_a_rule_is_a_miss(det, why):
    inj = ev.parse_ledger([_ledger("r1", T0)])[0]
    assert not ev.match_injection(inj, [det], SCENARIO).detected, why


def test_an_incident_rooted_on_the_device_counts_as_naming_it():
    inj = ev.parse_ledger([_ledger("r1", T0)])[0]
    d = _det(1, device=OTHER, at=T0 + timedelta(seconds=200), incident_root_device_id=CRAH)
    assert ev.match_injection(inj, [d], SCENARIO).detected


def test_the_poll_tolerance_rescues_a_boundary_detection_across_repeated_runs():
    """Three runs, three different phases against the poll cycle: one lands
    10 s before the window opens. A tolerance of one poll interval of the
    slowest metric absorbs that; without it the 3-of-3 gate fails."""
    entries = [_ledger(f"r{i}", T0 + timedelta(hours=i)) for i in (1, 2, 3)]
    inj = ev.parse_ledger(entries)
    dets = [_det(1, at=inj[0].start + timedelta(seconds=50)),
            _det(2, at=inj[1].start + timedelta(seconds=400)),
            _det(3, at=inj[2].start + timedelta(seconds=880))]
    strict = ev.score_scenario(SCENARIO, inj, dets)
    assert strict.runs == 3 and strict.detected_runs == 2
    loose = ev.score_scenario(SCENARIO, inj, dets, tolerance_s=30)
    assert loose.recall == 1.0
    assert loose.delay_median_s == 400
    assert loose.delay_range_s == (50, 880)


def test_kind_is_checked_only_when_asked():
    inj = ev.parse_ledger([_ledger("r1", T0)])[0]
    d = _det(1, at=T0 + timedelta(seconds=300), kind="process")
    assert ev.match_injection(inj, [d], SCENARIO).detected
    assert not ev.match_injection(inj, [d], SCENARIO, check_kind=True).detected


def test_repeated_raises_of_one_episode_count_once():
    dets = [_det(1, at=T0), _det(2, at=T0 + timedelta(seconds=60)),
            _det(3, at=T0 + timedelta(seconds=120), instance="B")]
    assert [d.id for d in ev.dedupe_episodes(dets)] == ["1", "3"]
    # With a gap, a re-raise a day later is a second episode.
    later = [*dets, _det(4, at=T0 + timedelta(days=1))]
    assert [d.id for d in ev.dedupe_episodes(later, gap_s=3600)] == ["1", "3", "4"]


def test_must_not_and_unmatched_detections_are_false_and_context_is_excluded():
    entries = [_ledger("r1", T0),
               {"id": "ctx1", "kind": "context", "device_id": None,
                "start": (T0 + timedelta(hours=3)).isoformat(),
                "end": (T0 + timedelta(hours=4)).isoformat(), "context": "sim_change"}]
    inj = ev.parse_ledger(entries)
    dets = [_det(1, at=T0 + timedelta(seconds=300)),                       # true
            _det(2, detector="CR-1", at=T0 + timedelta(seconds=300)),      # must_not
            _det(3, detector="DQ-4", device=OTHER, at=T0 + timedelta(hours=2)),  # unmatched
            _det(4, detector="DQ-4", device=OTHER, at=T0 + timedelta(hours=3, minutes=5))]
    s = ev.score_scenario(SCENARIO, inj, dets)
    assert sorted(s.false_ids) == ["2", "3"]
    assert s.precision == pytest.approx(1 / 3)
    assert s.runs == 1  # the context window is not a run


def test_an_absorbed_symptom_is_not_false():
    inj = ev.parse_ledger([_ledger("r1", T0)])
    dets = [_det(1, at=T0 + timedelta(seconds=300)),
            _det(2, detector="RM-2", device=OTHER, at=T0 + timedelta(seconds=400))]
    s = ev.score_scenario(SCENARIO, inj, dets, absorbed_ids=["2"])
    assert s.false_ids == [] and s.precision == 1.0


def test_lead_over_l1():
    at = T0 + timedelta(seconds=300)
    assert ev.lead_over_l1(at, [T0 + timedelta(seconds=900), T0 + timedelta(seconds=1200)]) == 600
    assert ev.lead_over_l1(at, [T0 + timedelta(seconds=100)]) == -200  # abrupt: negative
    assert ev.lead_over_l1(at, []) is None                             # "No L1"


def test_false_per_device_day_and_compression():
    assert ev.false_per_device_day(14, 700, 7) == pytest.approx(0.002857, abs=1e-6)
    assert ev.false_per_device_day(3, 0, 7) is None
    assert ev.compression_ratio(40, 4) == 10.0
    assert ev.compression_ratio(40, 0) is None


def test_percentile_is_linear():
    assert ev.percentile([1, 2, 3, 4, 5], 50) == 3
    assert ev.percentile([0, 10], 90) == 9
    assert ev.percentile([], 50) is None


def test_the_report_and_its_history_row(tmp_path):
    inj = ev.parse_ledger([_ledger("r1", T0)])
    s = ev.score_scenario(SCENARIO, inj, [_det(1, at=T0 + timedelta(seconds=300))])
    path = ev.write_report(s, tmp_path, now=T0)
    assert path.name == "2026-10-21-crah-supply-bias.json"
    assert json.loads(path.read_text())["recall"] == 1.0
    rows = list(csv.DictReader((tmp_path / "history.csv").open()))
    assert rows[0]["detector"] == "CR-5" and rows[0]["scenario"] == "crah-supply-bias"


def test_load_campaigns_reads_the_simulator_format(tmp_path):
    p = tmp_path / "anomaly_campaigns.json"
    p.write_text(json.dumps({"version": 1, "scenarios": [{
        "name": "crah-filter-loading", "tier": "gradual", "pending": "point_availability",
        "runs": 1, "expected": [{"detector": "CR-4", "kind": "process",
                                 "window_s": {"min": 21600, "max": 72000}}],
        "must_not": ["CR-1", "CR-2"]}]}))
    sc = ev.load_campaigns(p)["crah-filter-loading"]
    assert sc.expected_detectors == {"CR-4"} and sc.pending == "point_availability"
    assert sc.must_not == {"CR-1", "CR-2"}
    with pytest.raises(ev.CampaignsUnavailableError):
        ev.load_campaigns(tmp_path / "missing.json")


def test_the_cli_says_where_it_looked_when_the_file_is_absent(tmp_path, capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    try:
        import anomaly_eval
    finally:
        sys.path.pop(0)
    rc = anomaly_eval.main(["--campaigns", str(tmp_path / "nope.json"), "--out",
                            str(tmp_path), "score", "--campaign", "x"])
    assert rc == 2
    assert "nope.json" in capsys.readouterr().err


def test_a_re_raise_during_the_injection_is_the_same_episode_not_a_false_finding():
    inj = ev.parse_ledger([_ledger("r1", T0)])
    dets = [_det(1, at=T0 + timedelta(seconds=300)),
            _det(2, at=T0 + timedelta(seconds=1500))]   # same key, still injected
    s = ev.score_scenario(SCENARIO, inj, dets)
    assert s.recall == 1.0 and s.false_ids == [] and s.precision == 1.0
