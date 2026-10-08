"""The room's air as three horizontal planes, interpolated per aisle (docs/27 D5).

What a heat map of a hall may honestly show is the air the SENSORS saw,
spread sideways only where air actually mixes: along an aisle. A cold aisle
is drawn from the intake readings on the rack faces that open onto it; a hot
aisle from the exhaust readings on the faces that open onto it. The rack rows
between them are barriers, so an exhaust reading never bleeds into the cold
aisle beside it - a room-wide interpolation that blends the two is the usual
shortcut and it paints a hot cold-aisle that nobody measured.

Three heights, matching where ASHRAE puts intake sensors on a rack face:
bottom (~0.5 m), middle (~1.2 m), top (~1.8 m). The readings carry their own
height (the device's U, or a probe's mount height), so the planes show the
vertical gradient a rack really has rather than one colour stretched.

Method: inverse-distance weighting in three dimensions, power 2, within the
aisle, from the sensors within a few metres along the row. Every cell also
carries a confidence - how close its nearest sensor is - so the viewer FADES
the air nobody measured instead of painting confident colour over it.
Kriging would give that from the variogram; for v1 the distance does.

Pure Python on purpose: a hall is a few thousand cells and a hundred face
points, which runs in well under a second, and it keeps numpy out of a
backend that has not needed it.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass

#: One rack unit (EIA-310) and the height of U1's bottom edge above the floor -
#: the same figures the viewer draws racks with, so a sensor's height here is
#: the height it is drawn at.
U_M = 0.04445
RACK_BASE_M = 0.1
#: One texel per 0.3 m: half a tile, fine enough to show a hot rack and coarse
#: enough that a hall is a few thousand cells.
CELL_M = 0.3
#: The ASHRAE intake-sensor heights the planes are drawn at.
HEIGHTS_M = (0.5, 1.2, 1.8)
#: A rack face sits ON the aisle's edge; the stored band may stop a little
#: short of it. Readings this far outside the band still belong to it.
FACE_TOL_M = 0.45
#: Along the row, sensors further than this do not inform a cell.
REACH_M = 4.0
#: Confidence falls to zero when the nearest sensor is this far away.
FADE_M = 2.5


@dataclass(frozen=True)
class FacePoint:
    """An aggregated reading on a rack face: where, how high, how warm."""

    x: float
    y: float
    z: float
    temp_c: float
    #: 'intake' (front face, cold aisle) or 'exhaust' (rear face, hot aisle).
    side: str


@dataclass(frozen=True)
class Band:
    y_start: float
    y_end: float
    #: 'cold' | 'hot' | anything else (not drawn).
    kind: str


def face_points(x: float, y: float, readings: list[tuple[float, float]],
                side: str, rack_h_m: float) -> list[FacePoint]:
    """Collapse a face's readings into at most three points, one per third
    of the rack's height, each at the mean height and mean temperature of
    the readings in that third. A thousand server inlets become a few
    hundred face points without losing the gradient."""
    if not readings or rack_h_m <= 0:
        return []
    third = rack_h_m / 3.0
    tiers: list[list[tuple[float, float]]] = [[], [], []]
    for z, t in readings:
        i = min(2, max(0, int(z // third)))
        tiers[i].append((z, t))
    out = []
    for tier in tiers:
        if not tier:
            continue
        out.append(FacePoint(x=x, y=y, z=sum(z for z, _ in tier) / len(tier),
                             temp_c=sum(t for _, t in tier) / len(tier), side=side))
    return out


def _idw(points: list[FacePoint], xs: list[float], cx: float, cy: float, cz: float
         ) -> tuple[float | None, float]:
    """Interpolated temperature and the horizontal distance to the nearest
    informing point, from the points within REACH_M along the row."""
    lo = bisect_left(xs, cx - REACH_M)
    hi = bisect_right(xs, cx + REACH_M)
    if lo >= hi:
        return None, float("inf")
    num = den = 0.0
    nearest = float("inf")
    for p in points[lo:hi]:
        dx, dy, dz = p.x - cx, p.y - cy, p.z - cz
        d2 = dx * dx + dy * dy + dz * dz
        if d2 < 1e-6:
            return p.temp_c, 0.0
        w = 1.0 / d2
        num += w * p.temp_c
        den += w
        h = (dx * dx + dy * dy) ** 0.5
        if h < nearest:
            nearest = h
    return num / den, nearest


def field(width_m: float, depth_m: float, bands: list[Band], points: list[FacePoint],
          *, cell_m: float = CELL_M, heights_m: tuple[float, ...] = HEIGHTS_M) -> dict:
    """The three planes over the room grid.

    Returns the grid (`nx` by `ny`, row-major, y outward), and per plane a
    `temp` list (°C to 0.1, None where the cell is outside every drawn aisle
    or no sensor reaches it) and a `conf` list (0-1 to 0.01) of the same
    length. Cells under the racks and in aisles of unknown kind stay None:
    the map shows aisle air, which is the air that was measured.
    """
    nx = max(1, int(-(-width_m // cell_m)))
    ny = max(1, int(-(-depth_m // cell_m)))
    n = nx * ny
    by_side = {"intake": [p for p in points if p.side == "intake"],
               "exhaust": [p for p in points if p.side == "exhaust"]}
    planes: list[dict] = [{"height_m": h, "temp": [None] * n, "conf": [0.0] * n}
                          for h in heights_m]
    for b in bands:
        side = "intake" if b.kind == "cold" else "exhaust" if b.kind == "hot" else None
        if side is None or b.y_end <= b.y_start:
            continue
        pts = sorted((p for p in by_side[side]
                      if b.y_start - FACE_TOL_M <= p.y <= b.y_end + FACE_TOL_M),
                     key=lambda p: p.x)
        if not pts:
            continue
        xs = [p.x for p in pts]
        j0 = max(0, int(b.y_start // cell_m))
        j1 = min(ny - 1, int(b.y_end // cell_m))
        for j in range(j0, j1 + 1):
            cy = (j + 0.5) * cell_m
            if cy < b.y_start or cy > b.y_end:
                continue
            for i in range(nx):
                cx = (i + 0.5) * cell_m
                k = j * nx + i
                for plane in planes:
                    t, near = _idw(pts, xs, cx, cy, plane["height_m"])
                    if t is None:
                        continue
                    plane["temp"][k] = round(t, 1)
                    plane["conf"][k] = round(max(0.0, min(1.0, 1.0 - near / FADE_M)), 2)
    return {
        "cell_m": cell_m, "nx": nx, "ny": ny,
        "planes": planes,
        "intake_points": len(by_side["intake"]),
        "exhaust_points": len(by_side["exhaust"]),
    }
