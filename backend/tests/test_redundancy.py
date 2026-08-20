"""A/B side derivation for the power graph.

The property that matters most here is negative: a shared element must come
back with NO side. Everything the correlation logic does with this data depends
on being able to tell "this failure costs one path" from "this failure costs
both".
"""

from __future__ import annotations

from app.importer.redundancy import BOTH, derive_sides, edge_side, seed_side

# The real DC1 tree, which is a 2N distribution below two transfer switches
# hanging off a shared pair of switchgear lineups.
DEVICES = {
    "util": "UTIL1-DC1-UR",
    "swgr1": "SWGR1-DC1-UR", "swgr2": "SWGR2-DC1-GR",
    "gen1": "GEN1-DC1-GR", "gen2": "GEN2-DC1-GR",
    "ats1": "ATS1-DC1-UR", "ats2": "ATS2-DC1-UR",
    "upsa": "UPSA-DC1-UR", "upsb": "UPSB-DC1-UR",
    "mcc1": "MCC1-DC1-MR", "mcc2": "MCC2-DC1-MR",
    "mppa": "MPPA-DC1-HA", "mppb": "MPPB-DC1-HA",
    "rppa": "RPPA-DC1-HA-R1-04", "rppb": "RPPB-DC1-HA-R1-13",
    "pdua": "PDUA-DC1-HA-R1-01", "pdub": "PDUB-DC1-HA-R1-01",
    "srv": "SRV01-DC1-HA-R1-01",
    "crah": "CRAH1-DC1-HA-R9-05",
}
EDGES = [
    ("util", "swgr1"), ("gen1", "swgr2"), ("gen2", "swgr2"),
    ("swgr1", "ats1"), ("swgr1", "ats2"),
    ("swgr2", "ats1"), ("swgr2", "ats2"),
    ("ats1", "upsa"), ("ats1", "mcc1"),
    ("ats2", "upsb"), ("ats2", "mcc2"),
    ("mcc1", "mppa"), ("mcc2", "mppb"),
    ("mppa", "crah"),
    ("upsa", "rppa"), ("upsb", "rppb"),
    ("rppa", "pdua"), ("rppb", "pdub"),
    ("pdua", "srv"), ("pdub", "srv"),
]


def sides():
    return derive_sides(DEVICES, EDGES)


# --- seeding -----------------------------------------------------------------

def test_seed_reads_the_role_token_only():
    assert seed_side("PDUA-DC1-HA-R1-01") == "A"
    assert seed_side("RPPB-DC1-CP") == "B"
    assert seed_side("UPSA-DC1-UR") == "A"
    assert seed_side("MPPB-DC1-HA") == "B"


def test_seed_ignores_letters_that_are_not_the_role():
    """A rack or room called A must not make its contents side A."""
    assert seed_side("SRV01-DC1-HA-R1-01") is None      # HA is a hall, not a side
    assert seed_side("CRAH1-DC1-HA-R9-05") is None
    assert seed_side("ATS1-DC1-UR") is None             # numbered, not sided
    assert seed_side("SWGR1-DC1-UR") is None
    assert seed_side("") is None


# --- the safety property -----------------------------------------------------

def test_shared_switchgear_has_no_side():
    """SWGR1 and SWGR2 each feed BOTH transfer switches.

    They are the normal and emergency sources, not the A and B paths. Calling
    SWGR1 'A' would tell the correlation logic that losing utility power costs
    one path, when it costs both - which is the entire reason the generators
    are there.
    """
    s = sides()
    assert s["swgr1"] == BOTH
    assert s["swgr2"] == BOTH


def test_the_shared_trunk_above_the_switchgear_is_unlabelled():
    s = sides()
    for shared in ("util", "gen1", "gen2"):
        assert s.get(shared) not in ("A", "B"), (
            f"{DEVICES[shared]} feeds both paths and must not carry a side")


def test_a_dual_fed_load_is_on_both_paths():
    assert sides()["srv"] == BOTH


# --- derivation beyond the naming convention ---------------------------------

def test_transfer_switches_get_a_side_their_name_does_not_carry():
    """ATS1 is A because everything below it is, not because of its number."""
    s = sides()
    assert s["ats1"] == "A"
    assert s["ats2"] == "B"


def test_mechanical_distribution_inherits_from_the_panel_it_feeds():
    s = sides()
    assert s["mcc1"] == "A"      # feeds MPPA
    assert s["mcc2"] == "B"
    assert s["crah"] == "A"      # single-corded off the A mechanical panel


def test_derivation_is_independent_of_dictionary_order():
    """Regression: the first version froze a premature single-sided answer.

    Iterating in place let SWGR1 observe ATS1 resolve to A before ATS2 resolved
    to B, conclude 'A', and never revisit it - silently turning the shared
    utility feed into a single-path element, which is precisely the error this
    module exists to prevent.
    """
    forward = derive_sides(DEVICES, EDGES)
    reverse = derive_sides({k: DEVICES[k] for k in reversed(list(DEVICES))}, EDGES)
    assert forward == reverse
    assert forward["swgr1"] == BOTH


def test_both_is_absorbing():
    """Evidence of two paths is a conclusion, never narrowed back to one."""
    s = sides()
    # srv is fed by A and B; adding another A cord must not make it 'A'.
    more = derive_sides(DEVICES, [*EDGES, ("pdua", "srv")])
    assert s["srv"] == BOTH and more["srv"] == BOTH


# --- edges -------------------------------------------------------------------

def test_conductor_takes_the_feeder_side():
    """A cord leaving PDUA is on path A even though the server is on both."""
    s = sides()
    assert edge_side(s["pdua"], s["srv"]) == "A"
    assert edge_side(s["pdub"], s["srv"]) == "B"


def test_conductor_falls_back_to_what_it_feeds():
    """SWGR1 has no side, but its link to ATS1 still serves path A."""
    s = sides()
    assert edge_side(s["swgr1"], s["ats1"]) == "A"
    assert edge_side(s["swgr1"], s["ats2"]) == "B"


def test_the_shared_trunk_conductor_stays_unlabelled():
    # .get(): an unsided device has no entry at all, which is itself the point.
    s = sides()
    assert edge_side(s.get("util"), s.get("swgr1")) is None
    assert edge_side(s.get("gen1"), s.get("swgr2")) is None


def test_dual_fed_load_can_be_distinguished_from_single_feed():
    """The verdict 4.2 has to produce, from the edge sides alone."""
    s = sides()
    def feeding_sides(dev):
        return {edge_side(s.get(a), s.get(b)) for a, b in EDGES if b == dev} - {None}

    assert feeding_sides("srv") == {"A", "B"}     # N+1
    assert feeding_sides("crah") == {"A"}         # single feed
