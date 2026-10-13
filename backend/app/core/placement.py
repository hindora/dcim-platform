"""Where a rack or a floor-standing unit may stand: the room builder's rules
(docs/27 Phase 6).

Positions do not come from devices - no protocol reports where a rack stands -
so a DCIM holds them as a drawing somebody maintains. An edit here corrects
that drawing to match the floor, or records a move that has been carried out;
it moves nothing in the world. The rules are therefore the drawing's own:

* the footprint lies inside the room;
* it does not overlap another footprint. Racks in a row stand flank to flank,
  so touching is not overlapping - a tolerance absorbs the centimetre of
  rounding a stored coordinate carries.

Clearances a code would demand (service access in front of a switchboard, the
aisle widths of a containment design) are not judged: they depend on what the
equipment is and on local rules this module does not know, and a drawing tool
that refused a real, non-compliant floor could not be corrected to match it.

Pure functions, no database: the caller reads the room and its occupants.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Two footprints closer than this are touching, not overlapping. Stored
#: coordinates are Numeric(8, 2): a centimetre is the drawing's resolution.
TOUCH_M = 0.011

#: The raised-floor tile. Racks stand on this grid in every hall built on a
#: raised floor; the editor offers it as a snap, the server does not impose it,
#: because slab halls and plant rooms have no tiles.
TILE_M = 0.6


@dataclass(frozen=True)
class Box:
    """An axis-aligned footprint in room metres."""

    x0: float
    y0: float
    x1: float
    y1: float
    name: str = ""

    def overlaps(self, other: Box, tol: float = TOUCH_M) -> bool:
        return (self.x0 < other.x1 - tol and other.x0 < self.x1 - tol
                and self.y0 < other.y1 - tol and other.y0 < self.y1 - tol)


def footprint(x: float, y: float, w: float, d: float, rotation_deg: float | None = 0.0,
              name: str = "") -> Box:
    """The axis-aligned box a w x d footprint centred on (x, y) covers when
    turned `rotation_deg` about its centre.

    Quarter turns are exact. Any other angle gets the box that bounds the
    turned rectangle: slightly generous, which errs toward reporting a clash
    rather than hiding one.
    """
    a = math.radians((rotation_deg or 0.0) % 180.0)
    c, s = abs(math.cos(a)), abs(math.sin(a))
    half_w = (w * c + d * s) / 2.0
    half_d = (w * s + d * c) / 2.0
    return Box(x - half_w, y - half_d, x + half_w, y + half_d, name)


def snap(v: float, grid: float = TILE_M) -> float:
    """The nearest grid line, to the drawing's centimetre."""
    return round(round(v / grid) * grid, 2)


def problems(box: Box, room_w: float | None, room_d: float | None,
             others: list[Box]) -> list[str]:
    """Why this footprint cannot stand here; empty when it can.

    A room with no stored size has no walls to be outside of, so only the
    overlap rule applies - the same honesty the floor plan keeps when it draws
    an approximate outline.
    """
    out: list[str] = []
    if (room_w is not None and room_d is not None
            and (box.x0 < -TOUCH_M or box.y0 < -TOUCH_M
                 or box.x1 > room_w + TOUCH_M or box.y1 > room_d + TOUCH_M)):
        out.append(f"outside the room ({room_w:g} x {room_d:g} m)")
    hit = sorted(o.name for o in others if box.overlaps(o))
    if hit:
        shown = ", ".join(hit[:4]) + (f" and {len(hit) - 4} more" if len(hit) > 4 else "")
        out.append(f"overlaps {shown}")
    return out
