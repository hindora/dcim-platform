"""Configuration a collector is told, as opposed to the file it boots from.

The collector's own file keeps what lets it reach this platform: its id, the
API address, the token, Redis. Those stay on the host on purpose - break the
path to the control plane from the control plane and nobody can repair it from
the control plane either.

This table holds the operational half: which planes are on, how hard to poll
them, and where the inbound listeners sit. One row per collector, because two
collectors on different networks legitimately need different listen addresses
and the same estate.

`version` rather than a timestamp for the ETag: the collector compares what it
is running against what it was handed, and an integer it can report back in a
heartbeat is what makes "saved" and "in force" two answerable questions rather
than one assumed one.

Revision ID: 0034
Revises: 0033
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collector_config",
        sa.Column("collector_id", sa.Text(), primary_key=True),
        # Sparse on purpose: only what an operator actually set. Anything
        # absent falls through to the collector's file, so a default that
        # changes in a release reaches every collector that never overrode it.
        sa.Column("config", sa.dialects.postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("version", sa.Integer(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_by", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("collector_config")
