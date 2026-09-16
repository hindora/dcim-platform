"""Eleven server SKUs were rated below what one of their own PSUs supplies.

Revision ID: 0065
Revises: 0064

Found by putting a live draw next to a rating on the connectivity one-line
(docs/24 phase 2): thirty-nine of ninety servers in DC1 Server Hall A drew
MORE than their recorded nameplate, up to 177 %. Every other class in the
catalogue was comfortable - CRAH 6.6 %, PDU a median 22 %, CDU 49 %, UPS 6 % -
so it was the server rows, not the readings.

The clearest case: `Supermicro SYS-121H-TNR LCC`, recorded at 800 W, on a
chassis this database also records as carrying two 1100 W supplies. Nobody
fits 1100 W supplies to a box that draws 800 W. Eleven of the sixteen server
SKUs were in the same state, and they are the 2U, dual-socket, EPYC and
liquid-cooled rows - exactly the ones whose real figures have moved most since
a 1U Skylake box was the default.

WHAT THE NUMBER MEANS HERE. `rated_power_w` is the chassis's maximum rated
input power at the configuration modelled - the number a branch circuit is
sized against, and the denominator under every capacity bar and load
percentage in the product. It is not a measurement and it must not be derived
from one: a rating fitted to observed draw can never report an overload,
because it moves whenever the load does.

WHERE THESE FIGURES COME FROM, AND HOW FAR TO TRUST THEM. They are vendor
maximum-input figures for a two-socket build of each chassis. They are not
transcribed from a datasheet in front of me, and anyone who has the datasheet
should correct them - the point of this migration is that the previous values
were provably wrong (below the machine's own supplies), not that these are
exact. Two properties they do hold, and both are checkable:

  * every value is at or below the pair of PSUs the chassis carries (2 x 1100
    W, so 2200 W), and the highest here is 1800; and
  * every value clears the highest draw this estate has recorded for that SKU
    with headroom, so the catalogue no longer reports a fleet of servers in
    permanent overload. Three first-draft figures did NOT clear it and were
    raised: the R740 (peak 1330 W) to 1500, the DL380 Gen10 (peak 1185 W) to
    1400, and the DL380 Gen11 (peak 1311 W) to 1600. The tightest remaining
    margin is the R740 at 89 % of nameplate at its recorded peak, which is a
    2U machine working hard rather than a machine in trouble.

The five SKUs that were not over are raised too. `Dell PowerEdge R640` at
500 W was not causing a red bar, but a 1U two-socket server's maximum input is
not 500 W either, and a catalogue that is half corrected is harder to reason
about than one that is wrong consistently.

Rows are matched by vendor and model name and updated only where the value is
still the one this migration expects to replace, so a site that has already
corrected its own catalogue is left alone.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


# model name -> (old value this migration replaces, new maximum input W)
#
# Ordered as the audit printed them. The `old` column is what makes this
# re-runnable and safe on a catalogue somebody has already edited.
RATINGS: dict[str, tuple[int, int]] = {
    # 1U, two socket. Maximum input is bounded by the single supply that has
    # to carry the box when its partner is pulled.
    "Dell PowerEdge R640": (500, 750),
    "HPE ProLiant DL360 Gen10": (500, 750),
    "Lenovo ThinkSystem SR630 V2": (500, 750),
    # 2U, two socket, air cooled.
    "Dell PowerEdge R740": (650, 1500),
    "Dell PowerEdge R750": (750, 1400),
    "HPE ProLiant DL380 Gen10": (650, 1400),
    "HPE ProLiant DL380 Gen11": (750, 1600),
    "Lenovo ThinkSystem SR650 V2": (700, 1400),
    "Supermicro SYS-120U-TNR": (550, 1300),
    "Supermicro SYS-220U-TNR": (700, 1400),
    "IBM Power System S922": (1000, 1400),
    # Two socket EPYC: more cores, more memory channels, more power.
    "Dell PowerEdge R7525": (1000, 1600),
    # Direct liquid cooled. The reason these exist is a TDP an air path cannot
    # carry, so they are the top of the range rather than the middle.
    "Dell PowerEdge R660 DLC": (700, 1400),
    "Dell PowerEdge R760 DLC": (900, 1600),
    "Supermicro SYS-121H-TNR LCC": (800, 1600),
    "Supermicro SYS-221H-TNR LCC": (900, 1800),
}

_SQL = sa.text("""
    UPDATE model
       SET rated_power_w = :new
     WHERE name = :name
       AND device_type = 'server'
       AND rated_power_w = :old
""")


def _apply(pairs: list[tuple[str, int, int]]) -> None:
    conn = op.get_bind()
    for name, old, new in pairs:
        conn.execute(_SQL, {"name": name, "old": old, "new": new})


def upgrade() -> None:
    _apply([(name, old, new) for name, (old, new) in RATINGS.items()])


def downgrade() -> None:
    _apply([(name, new, old) for name, (old, new) in RATINGS.items()])
