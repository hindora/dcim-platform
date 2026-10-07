"""The estate has physical geometry: rooms placed in buildings, stored aisles,
rack footprints and the size, mount and facing of every device.

Geometry is the asset layer, never telemetry - no protocol reports where a rack
stands or how big a chiller is. Real DCIMs take it from CAD/BIM, a room builder
or a delivery team; here the simulator's floor-plan export stands in for that
import (docs/27, Phase 0). It is stored, not derived, because the floor plan used
to INVENT it: the room outline was the bounding box of the racks plus a margin,
the aisles were guessed from rack facing, and every CRAH, chiller and genset was
listed under the drawing because it had no coordinate.

- room: its corner in building coordinates on its level (`floor` already names
  the level), the level's height above grade, and a rotation.
- aisle: the aisles the room was drawn with - cold/hot, centre line, width,
  whether contained. Derivation from rack facing stays as the fallback.
- rack: footprint (width/depth/height); 600 x 1200 mm stays the fallback.
- device: how it is held (rack, zero_u, rack_front, floor, wall, pipe, panel),
  which way its front faces, its footprint if it stands on the floor, and how
  high its centre sits.

`geometry_source` is 'import' or 'manual'. Nothing writes 'manual' yet; when the
room builder does (docs/27 Phase 6), the importer stops overwriting that row's
geometry, so a re-import never undoes a correction someone drew by hand.

Building-level facts (levels, elevations, outline) live in
datacenter.attributes.building: they are derived from the rooms and have no
identity of their own yet.

Revision ID: 0099
Revises: 0098
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None

_M = sa.Numeric(8, 2)
_SMALL = sa.Numeric(6, 3)


def upgrade() -> None:
    for name, typ in (("origin_x_m", _M), ("origin_y_m", _M),
                      ("rotation_deg", sa.Numeric(6, 2)),
                      ("level_elevation_m", _M),
                      ("geometry_source", sa.Text())):
        op.add_column("room", sa.Column(name, typ, nullable=True))

    for name, typ in (("width_m", _SMALL), ("depth_m", _SMALL), ("height_m", _SMALL),
                      ("geometry_source", sa.Text())):
        op.add_column("rack", sa.Column(name, typ, nullable=True))

    for name, typ in (("mount", sa.Text()),
                      ("rotation_deg", sa.Numeric(6, 2)),
                      ("footprint_w_m", _SMALL), ("footprint_d_m", _SMALL),
                      ("height_m", _SMALL), ("mount_height_m", _SMALL),
                      ("footprint_basis", sa.Text()),
                      ("geometry_source", sa.Text())):
        op.add_column("device", sa.Column(name, typ, nullable=True))

    op.create_table(
        "aisle",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("room_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("room.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("y_m", _M, nullable=False),
        sa.Column("width_m", _M, nullable=False),
        sa.Column("between_rows", postgresql.ARRAY(sa.Integer), nullable=True),
        sa.Column("contained", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("room_id", "name"),
        sa.CheckConstraint("kind IN ('cold', 'hot')", name="aisle_kind"),
    )


def downgrade() -> None:
    op.drop_table("aisle")
    for name in ("geometry_source", "footprint_basis", "mount_height_m", "height_m",
                 "footprint_d_m", "footprint_w_m", "rotation_deg", "mount"):
        op.drop_column("device", name)
    for name in ("geometry_source", "height_m", "depth_m", "width_m"):
        op.drop_column("rack", name)
    for name in ("geometry_source", "level_elevation_m", "rotation_deg",
                 "origin_y_m", "origin_x_m"):
        op.drop_column("room", name)
