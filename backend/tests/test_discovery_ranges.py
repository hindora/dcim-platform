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

    monkeypatch.setattr(dr.repo, "get_ranges", get_ranges)
    monkeypatch.setattr(dr.run_repo, "create_run", create_run)
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
