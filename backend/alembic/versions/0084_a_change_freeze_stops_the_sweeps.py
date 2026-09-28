"""A change freeze stops discovery sweeps, estate-wide or for one site.

Change control freezes the floor before a migration, over year end, during an
audit: nothing touches the network that does not have to. A discovery sweep is
active traffic to every address in a range, and a scheduled one does not know
the freeze exists. So a blackout is a record - a window, estate-wide or one
site - that the scheduler skips inside (the skip is recorded as a run, so the
history says why nothing was swept) and that a hand-run sweep must explicitly
override.

`discovery_run.status` gains 'skipped' by use; it is free text.

Revision ID: 0084
Revises: 0083
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discovery_blackout",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        # NULL = every site. One site is the common case - a hall migration.
        sa.Column("datacenter_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("datacenter.id", ondelete="CASCADE")),
        sa.Column("reason", sa.Text),
        sa.Column("created_by", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("ends_at > starts_at", name="window"),
    )
    op.create_index("ix_discovery_blackout_window", "discovery_blackout",
                    ["starts_at", "ends_at"])


def downgrade() -> None:
    op.drop_index("ix_discovery_blackout_window", table_name="discovery_blackout")
    op.drop_table("discovery_blackout")
