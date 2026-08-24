"""Rooms carry the simulator's own white-space / facility classification.

`room_type` was inferred from the room NAME by the importer, which put
"Generator Room" and "Roof" in `data_hall` - there is no keyword in either to
match on. Every page that then asked "show me the halls" got a generator and a
roof, and any capacity maths restricted to data halls would have been counting
a tower deck as raised floor.

The simulator publishes the answer in its floor plan (`class: white_space |
facility`, with the room's extent and containment beside it), so the importer
now carries that through instead of guessing. This adds the column and
backfills what can be inferred safely in the meantime: a room is white space
only if it is typed as a hall or a network room AND actually contains racks -
which is exactly the pair of tests that excludes the generator room and the
roof.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: NULL means "nobody has classified this room yet", which is a
    # different statement from "this is a facility room" and the UI shows it as
    # such rather than filing an unknown room under plant.
    op.add_column("room", sa.Column("room_class", sa.Text(), nullable=True))
    op.add_column("room", sa.Column("designed_racks", sa.Integer(), nullable=True))

    op.execute("""
        UPDATE room rm
           SET room_class = CASE
               WHEN rm.room_type IN ('data_hall', 'network')
                    AND EXISTS (SELECT 1 FROM rack_row rr
                                 JOIN rack r ON r.row_id = rr.id
                                WHERE rr.room_id = rm.id)
               THEN 'white_space'
               ELSE 'facility'
           END
    """)

    op.create_index("ix_room_class", "room", ["room_class"])


def downgrade() -> None:
    op.drop_index("ix_room_class", table_name="room")
    op.drop_column("room", "designed_racks")
    op.drop_column("room", "room_class")
