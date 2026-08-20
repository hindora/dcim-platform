"""Redundancy verdicts.

The exit criterion for this phase is one of three words, and each way of
getting it wrong has a cost: a false N+1 tells someone a maintenance window is
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
    assert v == p.N_PLUS_1
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
    on an elevation and survives nothing. Calling that N+1 tells someone a
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
    be the same path twice. Claiming N+1 on that is a guess."""
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
