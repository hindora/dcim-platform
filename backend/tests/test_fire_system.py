"""The fire system as the BMS sees it: a category of its own, every condition
filed in it, the keys the three instruments publish, and rules at the
equipment's numbers (migration 0105)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

from app.core import alert_taxonomy as tx
from app.core.metrics_gen import METRICS
from app.importer.endpoints import PROBE_ROLE_BY_PREFIX

BACKEND = Path(__file__).resolve().parents[1]
TEMPLATES = yaml.safe_load(
    (BACKEND.parent / "contracts" / "mappings" / "modbus" / "templates.yaml").read_text(
        encoding="utf-8"
    )
)["templates"]


def _migration():
    spec = importlib.util.spec_from_file_location(
        "m0105", BACKEND / "alembic" / "versions" / "0105_a_fire_panel_reports_three_states.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fire_is_a_category_with_its_own_counter():
    assert tx.FIRE in tx.CATEGORIES
    assert tx.DESCRIPTIONS[tx.FIRE]["owner"] == "life safety"
    assert set(tx.DESCRIPTIONS) == set(tx.CATEGORIES)
    assert ("fire", "Fire", (tx.FIRE,)) in tx.STRIP_GROUPS
    assert tx.BY_METRIC_GROUP["fire"] == tx.FIRE


def test_every_seeded_condition_is_a_fire_condition():
    m = _migration()
    types = [r[1] for r in m.THRESHOLD_RULES] + [r[1] for r in m.STATE_RULES]
    assert len(types) == len(set(types)) == 9
    for alarm_type in types:
        # Whatever the instrument's role says, the type decides.
        assert tx.classify(alarm_type, role="environment", metric_key="x") == tx.FIRE, alarm_type
    for r in m.THRESHOLD_RULES:
        assert r[1] in tx.THRESHOLD_CROSSINGS
    for r in m.STATE_RULES:
        assert r[1] not in tx.THRESHOLD_CROSSINGS


def test_every_rule_reads_a_key_the_registry_has_on_a_sensor():
    m = _migration()
    for r in m.THRESHOLD_RULES:
        key = METRICS[r[2]]
        assert "sensor" in key.device_types and key.group == "fire" and key.value_type == "gauge", (
            r[0]
        )
    for r in m.STATE_RULES:
        key = METRICS[r[2]]
        assert "sensor" in key.device_types and key.group == "fire" and key.value_type == "bool", r[
            0
        ]


def test_the_smoke_rules_follow_the_detectors_stages_in_order():
    m = _migration()
    smoke = {r[1]: r for r in m.THRESHOLD_RULES if r[2] == "smoke_stage"}
    # Stage 1 Alert, 2 Action, 3 Fire 1: each rule trips on reaching its stage.
    assert [smoke[t][4] for t in ("smoke_alert", "smoke_action", "smoke_fire")] == [0.5, 1.5, 2.5]
    assert [smoke[t][6] for t in ("smoke_alert", "smoke_action", "smoke_fire")] == [
        "MINOR",
        "MAJOR",
        "CRITICAL",
    ]


def test_a_fire_alarm_and_a_release_are_not_debounced_and_trouble_is():
    m = _migration()
    dwell = {r[1]: r[3] for r in m.STATE_RULES}
    assert dwell["fire_alarm_active"] == 0 and dwell["gas_agent_released"] == 0
    assert dwell["fire_panel_trouble"] == 60 and dwell["fire_panel_supervisory"] == 60
    sev = {r[1]: r[4] for r in m.STATE_RULES}
    assert sev["fire_alarm_active"] == "CRITICAL" and sev["gas_agent_released"] == "CRITICAL"


def test_the_cylinder_rule_is_a_tenth_under_the_charge_in_the_stores_unit():
    m = _migration()
    (rule,) = [r for r in m.THRESHOLD_RULES if r[2] == "agent_pressure"]
    assert METRICS["agent_pressure"].unit == "kPa"
    assert rule[3] == "<" and rule[4] == 2250.0 and rule[5] > rule[4]  # clears above where it trips
    # The register is in bar x10; the template turns it into kPa.
    (pt,) = [p for p in TEMPLATES["SIM-GASREL-v1"]["points"] if p.get("metric") == "agent_pressure"]
    assert pt["scale"] == 10 and pt["factor"] == 100


def test_each_instrument_is_chosen_by_its_installed_role_and_names_its_metrics():
    want = {
        "SIM-FACP-GW-v1": (
            "fire_panel",
            {"fire_alarm", "fire_trouble", "fire_supervisory", "fire_zone_count"},
        ),
        "SIM-ASD-HLI-v1": ("smoke_aspirating", {"smoke_obscuration", "smoke_stage"}),
        "SIM-GASREL-v1": (
            "gas_release",
            {"gas_release_state", "gas_discharge_countdown", "agent_pressure", "agent_released"},
        ),
    }
    for map_id, (role, metrics) in want.items():
        t = TEMPLATES[map_id]
        # Role-selected: a second role-less sensor template takes the collector down.
        assert t["device_types"] == ["sensor"] and list(t["probe_roles"]) == [role], map_id
        got = {p["metric"] for p in t["points"] if p.get("metric")}
        assert metrics <= got, (map_id, metrics - got)
        for key in metrics:
            assert key in METRICS, key
    assert PROBE_ROLE_BY_PREFIX["FACP"] == "fire_panel"
    assert PROBE_ROLE_BY_PREFIX["ASD"] == "smoke_aspirating"
    assert PROBE_ROLE_BY_PREFIX["GSP"] == "gas_release"


def test_every_point_is_polled_into_a_metric_the_registry_has():
    # An unmapped point is emitted with an empty metric, which the collector
    # refuses at start and contracts/mapcheck.py refuses in CI.
    for map_id in ("SIM-FACP-GW-v1", "SIM-ASD-HLI-v1", "SIM-GASREL-v1"):
        for p in TEMPLATES[map_id]["points"]:
            if p.get("role") == "validity":
                continue
            assert p.get("metric") in METRICS, (map_id, p["name"])


def test_nothing_in_a_fire_template_is_writable():
    for map_id in ("SIM-FACP-GW-v1", "SIM-ASD-HLI-v1", "SIM-GASREL-v1"):
        spaces = {p["space"] for p in TEMPLATES[map_id]["points"]}
        assert spaces <= {"input", "discrete"}, (map_id, spaces)
