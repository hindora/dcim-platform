"""Discovery ranges: saved address space, swept by the collector that reaches it.

The failure modes are the tests. A range too wide for a sweep, a typo with host
bits set, an exclusion outside its range; a run for DC2's network taken by DC1's
collector; a collector that ignores exclusions handed a run that has them; an
excluded address later reported as gone or missing.
"""

from __future__ import annotations

import asyncio
import ipaddress
from pathlib import Path

import pytest

from app.services import discovery_ranges as dr

APP = Path(__file__).resolve().parents[1] / "app"
REPO = (APP / "repositories" / "discovery.py").read_text(encoding="utf-8")
RANGES_REPO = (APP / "repositories" / "discovery_ranges.py").read_text(encoding="utf-8")
COLLECTOR_API = (APP / "api" / "v1" / "collector.py").read_text(encoding="utf-8")
MIG = next((APP.parent / "alembic" / "versions").glob("0082_*.py")).read_text(
    encoding="utf-8")


def _body(src: str, name: str) -> str:
    start = src.index(f"async def {name}(")
    nxt = src.find("\nasync def ", start + 1)
    return src[start:nxt if nxt > 0 else len(src)]


# ---------------------------------------------------------------- validation

@pytest.mark.parametrize("ok", ["10.51.11.0/24", "10.51.8.0/22", "10.52.11.64/26",
                                "10.0.0.0/20", "10.0.0.7/32"])
def test_any_prefix_a_sweep_can_cover(ok):
    """Real management networks are /22s for a hall of BMCs and /26s per row, not
    tidy /24s."""
    assert str(dr.parse_cidr(ok)) == ok


def test_host_bits_are_answered_with_the_range_they_meant():
    with pytest.raises(dr.DiscoveryError, match=r"the range is 10\.51\.11\.0/24"):
        dr.parse_cidr("10.51.11.5/24")


@pytest.mark.parametrize("bad", ["", "10.51.11.0", "banana/24", "10.0.0.0/16",
                                 "fd00::/64"])
def test_what_a_sweep_cannot_do_is_refused(bad):
    """A /16 is refused rather than truncated: sweeping the first 4096 addresses of
    65,536 and reporting "found 12" would misstate what was audited."""
    with pytest.raises(dr.DiscoveryError):
        dr.parse_cidr(bad)


def test_the_widest_range_fits_one_sweep():
    net = dr.parse_cidr("10.0.0.0/20")
    assert dr.probe_count(net, []) <= dr.MAX_ADDRESSES


def test_exclusions_are_inside_the_range_and_collapsed():
    net = ipaddress.ip_network("10.51.11.0/24")
    assert dr.parse_exclusions(["10.51.11.1", "10.51.11.0/31", "10.51.11.200"], net) \
        == ["10.51.11.0/31", "10.51.11.200/32"]
    with pytest.raises(dr.DiscoveryError, match="outside"):
        dr.parse_exclusions(["10.51.12.1"], net)
    with pytest.raises(dr.DiscoveryError):
        dr.parse_exclusions(["gateway"], net)


def test_probe_count_matches_what_the_collector_sends():
    """Network and broadcast are skipped below /31, as Hosts() does; an exclusion
    over one of them does not subtract it twice."""
    net = ipaddress.ip_network("10.51.11.0/24")
    assert dr.probe_count(net, []) == 254
    assert dr.probe_count(net, ["10.51.11.1/32"]) == 253
    assert dr.probe_count(net, ["10.51.11.0/31"]) == 253  # .0 was never probed
    assert dr.probe_count(ipaddress.ip_network("10.0.0.4/31"), []) == 2


# ---------------------------------------------------------------- queueing

def _range(i, cidr, collector=None, exclusions=(), enabled=True):
    return {"id": f"00000000-0000-4000-8000-{i:012d}", "cidr": cidr, "name": cidr,
            "collector_id": collector, "exclusions": list(exclusions),
            "enabled": enabled}


def _queue(monkeypatch, ranges, **kw):
    made = []

    async def get_ranges(_s, ids):
        return [r for r in ranges if r["id"] in ids]

    async def create_run(_s, **run):
        made.append(run)
        return {"id": f"run-{len(made)}", **run}

    freezes = list(kw.pop("_blackouts", []))

    async def active_blackouts(_s):
        return freezes

    monkeypatch.setattr(dr.repo, "get_ranges", get_ranges)
    monkeypatch.setattr(dr.run_repo, "create_run", create_run)
    monkeypatch.setattr(dr.repo, "active_blackouts", active_blackouts)
    runs = asyncio.run(dr.queue(None, **kw))
    return runs, made


def test_each_collector_sweeps_only_its_own_ranges(monkeypatch):
    """The failure this exists to prevent: DC1's collector sweeping DC2's OOB
    network hears nothing, and the page reports a site of devices missing."""
    ranges = [_range(1, "10.51.11.0/24", "dc1-col"), _range(2, "10.51.21.0/24", "dc2-col"),
              _range(3, "10.51.12.0/24", "dc1-col")]
    _, made = _queue(monkeypatch, ranges, range_ids=[r["id"] for r in ranges])
    by = {m["collector_id"]: m["scope"]["subnets"] for m in made}
    assert by == {"dc1-col": ["10.51.11.0/24", "10.51.12.0/24"],
                  "dc2-col": ["10.51.21.0/24"]}


def test_a_selection_too_wide_for_one_sweep_is_several_whole_runs(monkeypatch):
    """Split, never truncated: every range is swept completely by some run."""
    ranges = [_range(i, f"10.{50 + i}.0.0/20") for i in range(3)]
    _, made = _queue(monkeypatch, ranges, range_ids=[r["id"] for r in ranges])
    assert len(made) == 3
    swept = [s for m in made for s in m["scope"]["subnets"]]
    assert sorted(swept) == sorted(r["cidr"] for r in ranges)


def test_exclusions_travel_with_the_run(monkeypatch):
    ranges = [_range(1, "10.52.11.0/24", exclusions=["10.52.11.1/32"])]
    _, made = _queue(monkeypatch, ranges, range_ids=[ranges[0]["id"]])
    assert made[0]["scope"]["exclude"] == ["10.52.11.1/32"]
    assert made[0]["range_ids"] == [ranges[0]["id"]]


def test_a_disabled_range_is_not_swept_by_hand(monkeypatch):
    ranges = [_range(1, "10.52.11.0/24", enabled=False)]
    with pytest.raises(dr.DiscoveryError, match="disabled"):
        _queue(monkeypatch, ranges, range_ids=[ranges[0]["id"]])


def test_a_one_off_subnet_goes_where_it_is_told(monkeypatch):
    _, made = _queue(monkeypatch, [], subnets=["10.99.0.0/24"], collector_id="dc2-col")
    assert made[0]["collector_id"] == "dc2-col" and made[0]["range_ids"] == []


def test_a_typo_fails_before_anything_is_queued(monkeypatch):
    with pytest.raises(dr.DiscoveryError):
        _queue(monkeypatch, [], subnets=["10.99.0.5/24"])


# ------------------------------------------------------------ claim routing

def test_a_run_goes_only_to_its_collector_or_to_any_if_unassigned():
    body = _body(REPO, "claim_pending")
    assert "collector_id IS NULL" in body
    assert "collector_id = CAST(:collector AS text)" in body


def test_a_collector_that_cannot_exclude_is_never_handed_exclusions():
    """An old collector would probe exactly the addresses somebody listed because
    probing them is harmful."""
    body = _body(REPO, "claim_pending")
    assert "COALESCE(scope -> 'exclude', '[]'::jsonb)) = 0" in body
    assert '"exclude" in supports' in COLLECTOR_API


def test_a_scoped_token_cannot_claim_as_another_collector():
    assert "declared collector id does not match the token" in COLLECTOR_API


# --------------------------------------------------- excluded is not asked

def test_an_excluded_address_never_reads_as_gone_or_missing():
    assert "unnest(CAST(:exclude AS text[]))" in _body(REPO, "mark_gone")
    assert "r.scope -> 'exclude'" in _body(REPO, "missing_devices")


# -------------------------------------------------------------- the record

def test_the_migration_holds_a_range_to_what_a_sweep_can_do():
    assert "family(cidr) = 4" in MIG
    assert "masklen(cidr) >= 20" in MIG
    assert "unique=True" in MIG


def test_existing_schedules_are_converted_not_dropped():
    assert "network(CAST(s AS inet))::cidr" in MIG, "host bits typed into a schedule"
    assert "SET enabled = false" in MIG, "a schedule nothing could convert is paused"


def test_a_range_a_schedule_uses_cannot_be_deleted():
    """Deleting it would quietly shrink what that schedule audits."""
    src = (APP / "services" / "discovery_ranges.py").read_text(encoding="utf-8")
    assert "schedules_using" in _body(src, "delete_range")


def test_suggestions_skip_space_a_range_already_covers():
    assert "NOT EXISTS (SELECT 1 FROM discovery_range r WHERE addr.a <<= r.cidr)" \
        in _body(RANGES_REPO, "suggestions")


# ------------------------------------------------------------------ cancel

SVC = (APP / "services" / "discovery.py").read_text(encoding="utf-8")
DISC_API = (APP / "api" / "v1" / "discovery.py").read_text(encoding="utf-8")


def test_only_an_unfinished_sweep_can_be_cancelled():
    """A finished run is history; cancelling it would rewrite what a sweep that
    happened concluded."""
    body = _body(REPO, "cancel_run")
    assert "prev.status IN ('pending', 'running')" in body
    assert "FOR UPDATE" in body


def test_a_cancelled_sweeps_results_are_discarded_not_recorded():
    """A collector older than the status route finishes a cancelled sweep and
    reports. Recording that against a run somebody stopped would be the opposite
    of what they asked for."""
    handler = COLLECTOR_API[COLLECTOR_API.index("async def discovery_results("):]
    assert handler.index("lock_run_status") < handler.index("record_results")
    assert 'if current != "running":' in handler
    assert '"discarded"' in handler


def test_a_sweep_can_learn_its_run_was_cancelled():
    """The collector asks while it sweeps, so a cancel stops the traffic rather
    than only discarding hours of it. Its claimant's business only, like results,
    and unlocked: the question must not wait behind a report."""
    handler = COLLECTOR_API[COLLECTOR_API.index("async def discovery_run_status("):
                            COLLECTOR_API.index("async def discovery_results(")]
    assert '"/discovery/{run_id}/status"' in COLLECTOR_API
    assert "run_claimant" in handler and "HTTP_403_FORBIDDEN" in handler
    assert "run_status" in handler and "HTTP_404_NOT_FOUND" in handler
    assert "FOR UPDATE" not in _body(REPO, "run_status")


def test_a_claim_names_what_must_be_probed_whatever_liveness_hears():
    """A collector skips addresses its liveness check hears nothing at. On a
    network ACL'd to SNMP only that is everything - so whatever inventory or an
    earlier sweep says is there is named, and probed in full, or it would read as
    missing (inventory) or gone (a candidate)."""
    claim = COLLECTOR_API[COLLECTOR_API.index("async def claim_discovery("):
                          COLLECTOR_API.index("class DiscoveryResult(")]
    assert 'run["expected"] = await disc_repo.expected_addresses(' in claim
    body = _body(REPO, "expected_addresses")
    assert "FROM device_endpoint e" in body and "e.enabled" in body
    assert "FROM discovery_candidate c" in body
    assert '_probed("k.a", "s.cidr")' in body   # network/broadcast never swept


def test_the_report_and_a_cancel_cannot_interleave():
    assert "FOR UPDATE" in _body(REPO, "lock_run_status")


def test_a_cancel_is_audited_and_a_finished_sweep_is_a_conflict():
    handler = DISC_API[DISC_API.index("async def cancel_run("):]
    assert '"discovery.run.cancel"' in handler
    assert "HTTP_409_CONFLICT" in handler


def test_cancelled_is_not_in_flight_so_schedules_can_fire_again():
    """The stuck run this exists for: a sweep waiting on a collector that never
    checks in held every schedule back, because a due schedule waits while any
    sweep is in flight."""
    assert "status IN ('pending', 'running')" in _body(REPO, "run_in_flight")
    assert "cancelled" not in _body(REPO, "run_in_flight")


# ------------------------------------------------ scheduling per collector

def test_a_busy_collector_holds_back_only_its_own_schedules():
    """A long sweep on DC1's collector held back DC2's schedule too."""
    body = _body(REPO, "run_in_flight")
    assert "collector_id = ANY(CAST(:ids AS text[]))" in body
    assert "CAST(:shared AS boolean) AND collector_id IS NULL" in body
    fire = _body(SVC, "fire_due_schedule")
    assert 'lanes = sorted({r["collector_id"] for r in live}' in fire
    # A schedule that must wait does not stop the next one firing.
    assert "continue" in fire[fire.index("run_in_flight(session, lanes)"):]


# ---------------------------------------------------------- stuck sweeps

from app.services import discovery as svc  # noqa: E402


def _run(**kw):
    base = {"id": "r1", "status": "pending", "scope": {"subnets": ["10.0.0.0/24"]},
            "collector_id": "dc2-col", "queued_s": 3600, "running_s": None,
            "collector_registered": True, "collector_age_s": 5,
            "freshest_collector_s": 5}
    return {**base, **kw}


def test_a_queue_behind_a_busy_collector_keeps_waiting():
    """A collector sweeps its runs one after another; a run behind two /20s waits
    hours and is not stuck."""
    assert svc.stuck_reason(_run(queued_s=6 * 3600)) is None


def test_a_run_for_a_collector_that_is_gone_gives_up_and_says_why():
    assert "never checked in" in svc.stuck_reason(
        _run(collector_registered=False, collector_age_s=None))
    assert "last checked in 2 h ago" in svc.stuck_reason(_run(collector_age_s=7200))
    # Not before it has waited long enough to be sure.
    assert svc.stuck_reason(_run(queued_s=60, collector_registered=False)) is None


def test_an_unassigned_run_gives_up_only_if_no_collector_is_about():
    assert svc.stuck_reason(_run(collector_id=None)) is None
    assert "no collector" in svc.stuck_reason(
        _run(collector_id=None, freshest_collector_s=7200))


def test_a_running_sweep_is_allowed_for_its_size():
    """A /24 gets the floor; a /20 - 4094 silent addresses on two protocols - gets
    hours, because it can legitimately take that long."""
    assert svc.running_allowance_s({"subnets": ["10.0.0.0/24"]}) == svc.RUNNING_FLOOR_S
    assert svc.running_allowance_s({"subnets": ["10.0.0.0/20"]}) > 6 * 3600
    ok = _run(status="running", running_s=1200)
    assert svc.stuck_reason(ok) is None
    late = _run(status="running", running_s=2 * 3600)
    assert "never reported" in svc.stuck_reason(late)


def test_exclusions_shrink_the_allowance():
    wide = svc.running_allowance_s({"subnets": ["10.0.0.0/20"]})
    carved = svc.running_allowance_s({"subnets": ["10.0.0.0/20"],
                                      "exclude": ["10.0.8.0/21"]})
    assert carved < wide


def test_a_stuck_sweep_fails_rather_than_cancels():
    """Nobody decided it; something broke. It should read as a fault."""
    assert "status = 'failed'" in _body(REPO, "fail_run")
    assert "FOR UPDATE OF r SKIP LOCKED" in _body(REPO, "unfinished_runs")
    sched = (APP / "services" / "discovery_scheduler.py").read_text(encoding="utf-8")
    assert sched.index("expire_stuck_runs") < sched.index("fire_due_schedule")


# ------------------------------------------------------------- change freezes

def _freeze(site=None):
    from datetime import UTC, datetime
    return {"id": "b1", "name": "Q4 freeze", "datacenter_id": site,
            "ends_at": datetime(2027, 1, 5, tzinfo=UTC), "active": True}


def test_a_freeze_refuses_a_hand_run_sweep_unless_overridden(monkeypatch):
    r = {**_range(1, "10.51.11.0/24"), "datacenter_id": "dc1"}
    with pytest.raises(dr.DiscoveryError, match="change freeze"):
        _queue(monkeypatch, [r], range_ids=[r["id"]], _blackouts=[_freeze("dc1")])
    _, made = _queue(monkeypatch, [r], range_ids=[r["id"]],
                     _blackouts=[_freeze("dc1")], override_blackout=True)
    assert len(made) == 1


def test_a_site_freeze_leaves_other_sites_alone(monkeypatch):
    r = {**_range(1, "10.52.11.0/24"), "datacenter_id": "dc2"}
    _, made = _queue(monkeypatch, [r], range_ids=[r["id"]], _blackouts=[_freeze("dc1")])
    assert len(made) == 1


def test_an_estate_freeze_covers_a_one_off_subnet_too():
    assert dr.frozen_by([_freeze(None)], None)
    assert dr.frozen_by([_freeze("dc1")], None) is None, "a site freeze needs a site"


def test_a_frozen_schedule_records_why_it_did_not_sweep():
    fire = _body(SVC, "fire_due_schedule")
    assert "record_skipped_run" in fire
    assert "override_blackout=True" in fire, "what is left after the freeze still sweeps"
    assert "'skipped'" in _body(REPO, "record_skipped_run")


# ---------------------------------------------------------------- alarms

ALARMS = (APP / "services" / "discovery_alarms.py").read_text(encoding="utf-8")


def test_findings_are_alarms_with_a_lifecycle():
    """Raised while the finding is open, cleared when it is resolved - nothing
    needs clearing by hand."""
    body = _body(ALARMS, "reconcile")
    assert "not in keys" in body and "'CLEARED'" in body
    assert "WHERE source = :source" in body, "only its own alarms are cleared"


def test_maintenance_and_firmware_drift_stay_quiet():
    body = _body(ALARMS, "desired")
    assert 'm["lifecycle"] == "maintenance"' in body
    assert "HARDWARE_FIELDS" in body


def test_the_platform_monitor_no_longer_clears_what_it_did_not_raise():
    """It cleared every open alarm with no device that was not in its findings -
    which included every discovery alarm about an unrecorded box."""
    alarms = (APP / "repositories" / "alarms.py").read_text(encoding="utf-8")
    assert alarms.count("AND source = 'platform'") == 2


def test_alarms_reach_the_ticketing_outbox():
    assert "outbox.enqueue_actions(session, actions)" in ALARMS
    col = (APP / "api" / "v1" / "collector.py").read_text(encoding="utf-8")
    assert "reconcile_and_enqueue" in col and "begin_nested" in col


def test_a_listed_type_is_ticketed_whatever_its_severity():
    """The live Jira policy tickets CRITICAL alarms only; a MINOR discovery alert
    was 'not ticketed'. Naming the type reaches it without widening the floor for
    every other alert."""
    from app.integrations import config as cfg
    from app.integrations import policy as pol
    base = cfg.resolved({"policy": {"min_severity": "CRITICAL"}})["policy"]
    alarm = {"alarm_type": "discovery_unrecorded", "response_class": "alert",
             "severity": "MINOR", "category": "visibility", "first_seen": None}
    assert pol.explain(alarm, {**base, "dwell_s": 0}).startswith("response class")
    listed = {**base, "dwell_s": 0, "alarm_types": ["discovery_unrecorded"]}
    assert pol.explain(alarm, listed) == pol.MATCH
    # Still no ticket for a symptom, listed or not.
    assert pol.explain({**alarm, "is_symptom": True}, listed) != pol.MATCH


def test_only_real_alarm_types_can_be_listed():
    from app.integrations import config as cfg
    with pytest.raises(cfg.IntegrationConfigError):
        cfg._policy({"alarm_types": ["not_a_condition"]})
    assert cfg._policy({"alarm_types": ["discovery_missing"]}) == {
        "alarm_types": ["discovery_missing"]}


def test_a_schedule_has_its_own_history():
    """The estate-wide run list keeps the last N; a busy estate scrolls a nightly
    schedule's runs off it within a day. Its history is asked for by id."""
    body = _body(REPO, "list_runs")
    assert "r.schedule_id = CAST(:schedule AS uuid)" in body
    assert "schedule_id=schedule_id" in DISC_API
    sched = _body(REPO, "list_schedules")
    assert "r.appeared AS last_appeared" in sched and "r.error AS last_error" in sched
