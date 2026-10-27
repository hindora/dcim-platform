"""Redundancy verdicts.

The exit criterion for this phase is one of three words, and each way of
getting it wrong has a cost: a false 2N tells someone a maintenance window is
safe when it is not, and a false single_feed sends them chasing a problem that
does not exist.
"""

from __future__ import annotations

from app.services import power as p


def hop(name: str, status: str = "ONLINE") -> p.Hop:
    return p.Hop(device_id=name, name=name, device_type="pdu", status=status)


def path(side: str | None, *names: str, status: str = "ONLINE",
         reaches: bool = True) -> p.Path:
    return p.Path(side=side, hops=[hop(n, status) for n in names],
                  reaches_source=reaches,
                  upstream_closure=set(names))


# --- the three verdicts ------------------------------------------------------

def test_two_live_sides_are_redundant():
    v, why = p.verdict([path("A", "PDUA", "UPSA"), path("B", "PDUB", "UPSB")])
    assert v == p.TWO_N
    assert v == "2N", "an A and a B feed are two systems, not one plus a spare"
    assert "A, B" in why


def test_one_feed_is_single_feed():
    v, why = p.verdict([path("A", "PDUA", "UPSA")])
    assert v == p.SINGLE_FEED
    assert "one live feed" in why


def test_nothing_feeding_it_is_no_feed():
    v, why = p.verdict([])
    assert v == p.NO_FEED
    assert "nothing" in why


# --- the mistake this exists to prevent --------------------------------------

def test_two_cords_on_the_same_side_are_not_redundant():
    """The finding worth having.

    A server cabled to two PDUs that are both on the A side looks dual-corded
    on an elevation and survives nothing. Calling that 2N tells someone a
    UPS swap is safe when it will drop the load.
    """
    v, why = p.verdict([path("A", "PDUA1", "UPSA"), path("A", "PDUA2", "UPSA")])
    assert v == p.SINGLE_FEED
    assert "cabled twice, protected once" in why


def test_a_dead_hop_breaks_the_path_through_it():
    """Power does not route around a failed UPS the way a packet routes
    around a failed switch."""
    live = path("A", "PDUA", "UPSA")
    dead = p.Path(side="B", reaches_source=True,
                  hops=[hop("PDUB"), hop("UPSB", "OFFLINE")])
    v, why = p.verdict([live, dead])
    assert v == p.SINGLE_FEED
    assert "1 other feed(s) broken" in why


def test_every_path_broken_is_no_feed_and_names_the_failure():
    dead_a = p.Path(side="A", reaches_source=True,
                    hops=[hop("PDUA"), hop("UPSA", "OFFLINE")])
    dead_b = p.Path(side="B", reaches_source=True,
                    hops=[hop("PDUB"), hop("UPSB", "OFFLINE")])
    v, why = p.verdict([dead_a, dead_b])
    assert v == p.NO_FEED
    assert "UPSA" in why or "UPSB" in why


def test_a_path_that_reaches_no_source_is_not_a_feed():
    """A chain that dead-ends in the graph is missing data, not a supply."""
    v, _ = p.verdict([path("A", "PDUA", reaches=False)])
    assert v == p.NO_FEED


def test_undetermined_sides_do_not_establish_redundancy():
    """Two feeds whose paths could not be sided might be independent or might
    be the same path twice. Claiming 2N on that is a guess."""
    v, why = p.verdict([path(None, "X"), path(None, "Y")])
    assert v == p.SINGLE_FEED
    assert "undetermined" in why


# --- shared upstream ---------------------------------------------------------

def test_shared_upstream_names_the_common_mode_points():
    """A 2N load is only 2N below where its paths diverge."""
    a = p.Path(side="A", reaches_source=True, hops=[hop("UPSA"), hop("SWGR")],
               upstream_closure={"UPSA", "SWGR", "UTIL"})
    b = p.Path(side="B", reaches_source=True, hops=[hop("UPSB"), hop("SWGR")],
               upstream_closure={"UPSB", "SWGR", "UTIL"})
    shared = {h.name for h in p.shared_hops([a, b], {"UTIL": hop("UTIL")})}
    assert "SWGR" in shared
    assert "UTIL" in shared, "a common element off the displayed chain was dropped"
    assert "UPSA" not in shared


def test_a_single_path_has_nothing_shared():
    assert p.shared_hops([path("A", "PDUA")]) == []


# --- the census, split ---------------------------------------------------------

def _load(t, name, room="h", site="DC1"):
    return {"device_type": t, "name": name, "room_id": room, "room_name": f"room-{room}",
            "site_code": site}


def test_the_census_counts_it_by_cords_and_plant_by_members_fed():
    """Fifty chillers and CRAHs on one cord each are the design, not fifty
    findings. They become groups judged on how many members still have a feed."""
    loads = {
        "s1": _load("server", "SRV1"), "s2": _load("server", "SRV2"),
        "s3": _load("server", "SRV3"),
        "c1": _load("chiller", "CHL1", room="cp"), "c2": _load("chiller", "CHL2", room="cp"),
        "c3": _load("chiller", "CHL3", room="cp"),
        "a1": _load("crah", "CRAH1"), "a2": _load("crah", "CRAH2"),
    }
    verdicts = {
        "s1": (p.TWO_N, ""), "s2": (p.SINGLE_FEED, ""), "s3": (p.NO_FEED, ""),
        "c1": (p.SINGLE_FEED, ""), "c2": (p.SINGLE_FEED, ""), "c3": (p.NO_FEED, ""),
        "a1": (p.SINGLE_FEED, ""), "a2": (p.SINGLE_FEED, ""),
    }
    out = p.census(loads, verdicts)
    assert out["it_loads"] == {p.TWO_N: 1, p.SINGLE_FEED: 1, p.NO_FEED: 1}
    groups = {(g["room_id"], g["device_type"]): g for g in out["plant_groups"]}
    chillers = groups[("cp", "chiller")]
    assert chillers["n"] == 3 and chillers["fed"] == 2
    assert chillers["verdict"] == p.GROUP_TIGHT and chillers["unfed"] == ["CHL3"]
    assert groups[("h", "crah")]["verdict"] == p.GROUP_OK
    assert out["plant_groups_short"] == 1
    # Worst first.
    assert out["plant_groups"][0]["device_type"] == "chiller"


def test_a_plant_group_two_down_is_short():
    assert p.plant_group_verdict(4, 4) == (p.GROUP_OK, "all 4 fed")
    assert p.plant_group_verdict(4, 3)[0] == p.GROUP_TIGHT
    assert p.plant_group_verdict(4, 2)[0] == p.GROUP_SHORT


def test_a_panel_read_through_its_meter_is_fine_until_the_meter_goes_stale():
    row = {"verdict": "unmonitored", "why": None, "load_source": "metered_by",
           "metered_by": "EV21-DC1-HA", "metered_stale": False, "load_pct": 40.0}
    assert p.supply_verdict(row) == ("ok", "read through EV21-DC1-HA")
    row["metered_stale"] = True
    v, why = p.supply_verdict(row)
    assert v == "silent" and "older than 15 minutes" in why


def test_a_board_near_its_rating_is_a_finding_the_plant_readers_cannot_make():
    v, why = p.supply_verdict({"verdict": "ok", "why": None, "load_pct": 92.0})
    assert v == "load_high" and "breaker" in why
    v, why = p.supply_verdict({"verdict": "ok", "why": None, "load_pct": 85.0})
    assert v == "load_high" and "failover" in why
    # The plant's own finding wins over a load figure.
    assert p.supply_verdict({"verdict": "on_battery", "why": "x", "load_pct": 95.0}) \
        == ("on_battery", "x")
    assert p.supply_verdict({"verdict": "ok", "why": None, "load_pct": 40.0}) == ("ok", None)


def test_a_transformer_on_natural_cooling_is_not_standby():
    v, why = p.supply_verdict({"device_type": "transformer", "verdict": "standby",
                               "why": "healthy and not running", "load_pct": 9.0})
    assert v == "ok" and "natural cooling" in why
    # A generator on standby is on standby.
    assert p.supply_verdict({"device_type": "generator", "verdict": "standby",
                             "why": "x", "load_pct": 0.0})[0] == "standby"


def test_a_ups_with_minutes_of_battery_against_a_seconds_generator_is_short():
    v, why = p.supply_verdict({"device_type": "ups", "verdict": "ok", "why": None,
                               "battery_minutes": 3.2, "load_pct": 40.0})
    assert v == "runtime_short" and "15 s generator window" in why
    # Plenty of runtime: the plant's own verdict stands.
    assert p.supply_verdict({"device_type": "ups", "verdict": "ok", "why": None,
                             "battery_minutes": 113.0, "load_pct": 40.0}) == ("ok", None)
    # On battery outranks a short runtime: it is the finding that explains it.
    assert p.supply_verdict({"device_type": "ups", "verdict": "on_battery", "why": "x",
                             "battery_minutes": 3.0})[0] == "on_battery"
