"""A nightly snapshot of the estate, so trends have something to be drawn from.

Every trend chart the asset pages were asked for - item count over time, free
rack units over time, the month-on-month delta - failed on the same fact:
nothing records history. `device_lifecycle_event` accrues from the day it
started being written, and capacity has no memory at all. A line drawn through
one point says something it cannot know.

One row per day. WIDE TYPED COLUMNS, not a jsonb blob, because a snapshot
exists to be read DOWN a column - "u_used for the last 90 days" - and jsonb
hides the types and the indexes that query wants. Each column here is a trend
somebody may reasonably chart.

The day is the PRIMARY KEY, which is what makes the writer idempotent: two
ingest workers, a restart mid-tick, a manual run - all collapse into INSERT ..
ON CONFLICT (day) DO NOTHING. A snapshot that could be taken twice would show a
day disagreeing with itself.

What this deliberately does NOT carry: installs and decommissions. Those come
from device_lifecycle_event, because a snapshot DIFF conflates them - a day
with 10 installs and 10 decommissions nets to zero and the activity vanishes.
Counts of state are snapshotted; movements between states are events.

Revision ID: 0050
Revises: 0049
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_snapshot",
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        # The estate, by state. Every lifecycle state gets a column so a state
        # that empties still reads zero rather than vanishing from history.
        sa.Column("devices", sa.Integer, nullable=False),
        sa.Column("planned", sa.Integer, nullable=False, server_default="0"),
        sa.Column("in_stock", sa.Integer, nullable=False, server_default="0"),
        sa.Column("installed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("in_service", sa.Integer, nullable=False, server_default="0"),
        sa.Column("maintenance", sa.Integer, nullable=False, server_default="0"),
        sa.Column("decommissioned", sa.Integer, nullable=False, server_default="0"),
        sa.Column("retired", sa.Integer, nullable=False, server_default="0"),
        # Capacity. u_held is planned placeholders - spoken for, not free, and
        # folding it into either would repeat the mistake the overview avoids.
        sa.Column("racks", sa.Integer, nullable=False),
        sa.Column("u_total", sa.Integer, nullable=False),
        sa.Column("u_used", sa.Integer, nullable=False),
        sa.Column("u_held", sa.Integer, nullable=False, server_default="0"),
        # Identity and cover, because "when did the serials arrive" and "how
        # fast is cover eroding" are trends somebody will eventually ask for,
        # and a column not snapshotted is a question that can never be answered
        # about the past.
        sa.Column("with_serial", sa.Integer, nullable=False, server_default="0"),
        sa.Column("with_asset_tag", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cover_active", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cover_expiring", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cover_expired", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cover_unknown", sa.Integer, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    # History goes with the table, and that is the honest shape of the
    # rollback: there is nowhere else these rows could live.
    op.drop_table("asset_snapshot")
