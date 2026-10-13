"""The site has a compound.

docs/27 §8b and §8c. A site was its building and nothing else; the building
was its rooms and nothing else. Two kinds of thing now stand beside the rooms:

* `space`: building fabric and compound zones - corridor, stair core, lobby,
  dock, door openings; fence, gate, road, apron, car park, substation,
  generator yard, fuel and water tanks, fire pump house, telecom entrances.
  Drawn, never coloured by state, because they have none. A zone that holds
  equipment is also a `room` (class `outdoor`, level "Site"), and `room_id`
  points at it. The importer owns these rows (`source = 'import'`) and
  replaces them on every import; a hand-drawn one would carry another source.

* `room.room_class` admits two more words besides `white_space` and
  `facility`: `support` (a store, the operations room, the meet-me room -
  assets or a sensor, no IT racks) and `outdoor` (a yard with equipment). The
  column is free text and always was; the readers that said "facility" where
  they meant "not white space" were corrected alongside this migration.

The compound's outline and extent sit in `datacenter.attributes.compound`,
beside `.building`, since like the building they are derived by the source
and have no identity of their own.

Revision ID: 0102
Revises: 0101
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "space",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("datacenter_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("datacenter.id", ondelete="CASCADE"), nullable=False),
        # The source's own id for the space ("DC1/G/corridor"), so an import
        # replaces rather than duplicates.
        sa.Column("external_key", sa.Text(), nullable=False),
        # A level name for indoor fabric; "Site" for the compound.
        sa.Column("level", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        # [[x, y], ...] in the site's plan metres, the building's corner the datum.
        sa.Column("polygon_m", postgresql.JSONB(), nullable=False),
        sa.Column("room_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("room.id", ondelete="SET NULL"), nullable=True),
        sa.Column("attributes", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("source", sa.Text(), nullable=False, server_default="import"),
        sa.UniqueConstraint("datacenter_id", "external_key", name="uq_space_key"),
    )
    op.create_index("ix_space_datacenter", "space", ["datacenter_id"])


def downgrade() -> None:
    op.drop_index("ix_space_datacenter", table_name="space")
    op.drop_table("space")
