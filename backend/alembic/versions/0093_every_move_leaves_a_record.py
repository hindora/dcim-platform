"""Every ownership move leaves a record: endpoint_assignment_history.

docs/26 Phase 5's shard map asks for "endpoint -> collector with history",
and the plan's own reason for persisting ownership at all was audit - "who
polled this UPS at 03:12?". endpoint_assignment answers "who owns it now"
and nothing else: each move overwrites the row, so the answer to the audit
question was gone the moment the next move happened.

One row per change of owner, written by the same statement that moves the
endpoint (repositories/collector.write_assignment), so a move and its record
cannot disagree. Moves are rare by construction - the assigner writes only
real changes and damps ordinary rebalances - so this grows with operator
actions and failures, not with time.

Seeded from endpoint_assignment's current rows, as their last recorded move
(from unknown, at `since`): the history before this migration was never
kept, and saying so with a NULL `from_collector` is more honest than an
empty map for an estate that plainly has owners.

No CHECK on reason, unlike endpoint_assignment: a history row outlives the
vocabulary it was written in, and 0090 already had to widen that constraint
once. The live table's constraint is what guards new values.

Revision ID: 0093
Revises: 0092
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "endpoint_assignment_history",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("device_endpoint.id", ondelete="CASCADE"),
                  nullable=False),
        # NULL: unowned (pool_empty), or unknown for the seeded rows below.
        sa.Column("from_collector", sa.Text),
        sa.Column("to_collector", sa.Text),
        sa.Column("epoch", sa.BigInteger, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_assignment_history_endpoint_at",
                    "endpoint_assignment_history", ["endpoint_id", sa.text("at DESC")])
    # For "moves in the last day" and for pruning by age.
    op.create_index("ix_assignment_history_at", "endpoint_assignment_history", ["at"])
    op.execute("""
        INSERT INTO endpoint_assignment_history
               (endpoint_id, from_collector, to_collector, epoch, reason, at)
        SELECT ea.endpoint_id, NULL, ea.collector_id, ea.epoch, ea.reason, ea.since
          FROM endpoint_assignment ea
          JOIN device_endpoint e ON e.id = ea.endpoint_id
    """)


def downgrade() -> None:
    op.drop_index("ix_assignment_history_at", table_name="endpoint_assignment_history")
    op.drop_index("ix_assignment_history_endpoint_at",
                  table_name="endpoint_assignment_history")
    op.drop_table("endpoint_assignment_history")
