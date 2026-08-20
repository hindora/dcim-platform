"""Room floor plan: rack positions, derived room extent, and aisle bands.

Coordinates come from the source in metres. Two things the source does NOT
give us, handled explicitly rather than fudged:

* **Room dimensions.** width_m and depth_m are null for every room, so the
  outline is derived from the bounding box of everything placed in the room
  plus a margin. It is labelled derived, because a floor plan that silently
  invents a wall in the wrong place is worse than one that admits the wall is
  approximate.

* **Rack footprint.** Not stored per rack. A 600 x 1200 mm cabinet is the
  standard EIA/IEC size and is used as the default - an assumption about the
  hardware, not about this particular room.

The aisle bands are not decoration. Hot and cold aisle containment is the
single most consequential fact about a room's airflow, and it is derivable:
racks that face INTO the gap between two rows make that gap a cold aisle,
because their intakes are there. Racks that put their backs to it make it a
hot aisle.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import Any

# Standard cabinet footprint, in metres. Not read from the database - it is not
# stored - so it is an assumption about the hardware and is reported as one.
RACK_W = 0.6
RACK_D = 1.2

# Clearance added around the equipment bounding box to stand in for walls.
MARGIN = 0.6

# 'N' faces lower y, 'S' faces higher y - the source's convention, preserved
# through the importer because it is the rack's real orientation in the hall.
FACES_LOWER_Y = "N"
FACES_HIGHER_Y = "S"


@dataclass
class Aisle:
    y_start: float
    y_end: float
    kind: str          # "cold" | "hot" | "unknown"
    label: str | None
    rows: list[str]


def room_extent(points: list[tuple[float, float]]) -> dict[str, Any]:
    """A room outline inferred from what is standing in it."""
    if not points:
        return {"width_m": 0.0, "depth_m": 0.0, "derived": True}
    max_x = max(x for x, _ in points)
    max_y = max(y for _, y in points)
    return {
        # Half a footprint past the furthest centre, then a margin for the wall.
        "width_m": round(max_x + RACK_W / 2 + MARGIN, 2),
        "depth_m": round(max_y + RACK_D / 2 + MARGIN, 2),
        "derived": True,
    }


def _row_facing(facings: set[str]) -> str | None:
    """One orientation for a row, or None when its racks disagree.

    A row whose racks face different ways has no single front, and guessing one
    would put the cold aisle on the wrong side of it.
    """
    real = {f for f in facings if f in (FACES_LOWER_Y, FACES_HIGHER_Y)}
    return real.pop() if len(real) == 1 else None


def derive_aisles(racks: list[dict[str, Any]]) -> list[Aisle]:
    """Classify the gap between each pair of adjacent rows.

    Cold when both rows face into the gap (intakes meet across it), hot when
    both put their backs to it. Anything else is left unknown rather than
    guessed: a mislabelled aisle sends someone to look for a hot spot on the
    wrong side of a row.
    """
    rows: dict[float, dict[str, Any]] = {}
    for r in racks:
        y = r.get("floor_y")
        if y is None:
            continue
        band = rows.setdefault(float(y), {"facings": set(), "names": set(),
                                          "cold": set(), "hot": set()})
        band["facings"].add(r.get("facing") or "")
        if r.get("row_name"):
            band["names"].add(r["row_name"])
        if r.get("cold_aisle"):
            band["cold"].add(r["cold_aisle"])
        if r.get("hot_aisle"):
            band["hot"].add(r["hot_aisle"])

    ordered = sorted(rows.items())
    out: list[Aisle] = []
    for (y_a, a), (y_b, b) in pairwise(ordered):
        face_a, face_b = _row_facing(a["facings"]), _row_facing(b["facings"])
        # The gap runs from the back/front edge of one row to that of the next.
        start = y_a + RACK_D / 2
        end = y_b - RACK_D / 2
        if end <= start:
            continue        # rows abut; there is no aisle between them

        kind, label = "unknown", None
        if face_a == FACES_HIGHER_Y and face_b == FACES_LOWER_Y:
            kind = "cold"                       # both face into the gap
            label = next(iter(a["cold"] | b["cold"]), None)
        elif face_a == FACES_LOWER_Y and face_b == FACES_HIGHER_Y:
            kind = "hot"                        # both back onto it
            label = next(iter(a["hot"] | b["hot"]), None)

        out.append(Aisle(y_start=round(start, 3), y_end=round(end, 3),
                         kind=kind, label=label,
                         rows=sorted(a["names"] | b["names"])))
    return out
