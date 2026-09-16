"""The server nameplate table in migration 0065 has to stay self-consistent.

A rating is the denominator under every capacity bar and load percentage in
the product. The two properties below are what make the table defensible
without a datasheet to hand, and they are cheap to keep true - which matters,
because the way this went wrong the first time was a number nobody checked
against anything.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
         / "0065_a_server_nameplate_below_its_own_psu.py")
_spec = importlib.util.spec_from_file_location("m0065", _PATH)
m0065 = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(m0065)

# What the chassis in this estate carry: two 1100 W supplies.
PSU_PAIR_W = 2200

# The highest draw recorded per SKU when the ratings were audited, 2026-09-16.
# Not live data - a snapshot, so the table can be checked without a database.
# A SKU whose real peak has since passed its rating is a finding for the page,
# not a failure here; this pins that the table was correct when it was written.
PEAK_W = {
    "Dell PowerEdge R640": 421,
    "HPE ProLiant DL360 Gen10": 416,
    "Lenovo ThinkSystem SR630 V2": 421,
    "Dell PowerEdge R740": 1330,
    "Dell PowerEdge R750": 1129,
    "HPE ProLiant DL380 Gen10": 1185,
    "HPE ProLiant DL380 Gen11": 1311,
    "Lenovo ThinkSystem SR650 V2": 1179,
    "Supermicro SYS-120U-TNR": 984,
    "Supermicro SYS-220U-TNR": 1166,
    "IBM Power System S922": 833,
    "Dell PowerEdge R7525": 1166,
    "Dell PowerEdge R660 DLC": 926,
    "Dell PowerEdge R760 DLC": 1179,
    "Supermicro SYS-121H-TNR LCC": 994,
    "Supermicro SYS-221H-TNR LCC": 1305,
}


def test_no_sku_claims_more_than_its_supplies_can_deliver():
    """A chassis cannot draw more than the supplies fitted to it, so a
    nameplate above them is not a rating - it is a typo with authority."""
    for name, (_old, new) in m0065.RATINGS.items():
        assert new <= PSU_PAIR_W, f"{name} rated {new} W on {PSU_PAIR_W} W of supplies"


def test_every_sku_clears_its_recorded_peak():
    """The defect being fixed. A nameplate below the machine's own observed
    draw reports permanent overload, and a page that is always red stops being
    read."""
    for name, (_old, new) in m0065.RATINGS.items():
        peak = PEAK_W[name]
        assert new > peak, f"{name} rated {new} W but has drawn {peak} W"


def test_the_corrected_ratings_leave_real_headroom():
    """Clearing the peak by a watt is not clearing it. Anything sitting above
    the 95 % critical band at its own recorded peak would light up the moment
    the estate got busy, which is the same failure in a smaller coat."""
    for name, (_old, new) in m0065.RATINGS.items():
        assert PEAK_W[name] / new < 0.95, (
            f"{name} peaks at {PEAK_W[name] / new:.0%} of its new rating")


def test_every_rating_is_actually_being_raised():
    """A no-op row is a row somebody will later mistake for a decision."""
    for name, (old, new) in m0065.RATINGS.items():
        assert new > old, f"{name} is not raised: {old} -> {new}"


def test_the_table_covers_the_whole_server_catalogue():
    """Sixteen server SKUs were audited and all sixteen are here. A catalogue
    corrected in part is harder to reason about than one wrong throughout."""
    assert set(m0065.RATINGS) == set(PEAK_W)
