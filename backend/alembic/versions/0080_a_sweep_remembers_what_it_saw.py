"""A sweep remembers what it saw, what changed, and when the next one is due.

Three things the discovery audit could not say.

1. WHAT A RUN FOUND, AFTER THE FACT. A run's counts were computed from each
   candidate's CURRENT run_id, and the upsert moves that to whichever run saw the
   candidate last. So a sweep's "29 answered, 29 expected" shrank to zero the next
   time the range was swept: history rewritten by the next observation. The counts
   are now written onto the run when it finishes, and the old live computation is
   only a fallback for runs recorded before this migration.

   `appeared`, `gone` and `changed` are new: what is different since the previous
   look, which is the question somebody reading last night's sweep arrives with.

   `claimed_at` is when a collector actually started, as opposed to `started_at`,
   which is when the run was queued. Silence is judged against the moment the
   sweep began asking; with scheduled runs a queued one can wait behind another,
   and judging from the queue time under-marks what went quiet.

2. WHAT CHANGED ABOUT A RESPONDER. One row per field per change, kept rather than
   overwritten: a serial that differs at the same address is a box that was
   replaced without anybody recording it, and a firmware string that moved is a
   change somebody should be able to date. Acknowledged rather than deleted.

3. WHEN TO LOOK AGAIN. Per-range schedules. An interval, not a cron expression -
   "nightly" and "weekly" are what ranges are swept on - and a next_run_at the
   scheduler advances, so a missed tick runs late rather than never.

Revision ID: 0080
Revises: 0079
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------ run snapshot
    for col in ("known", "unknown", "moved", "with_serial",
                "appeared", "gone", "changed"):
        op.add_column("discovery_run", sa.Column(col, sa.Integer))
    op.add_column("discovery_run",
                  sa.Column("claimed_at", sa.DateTime(timezone=True)))

    # ------------------------------------------------------------- schedules
    op.create_table(
        "discovery_schedule",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("subnets", postgresql.ARRAY(sa.Text), nullable=False),
        # At least an hour: a sweep of a /24 takes minutes, and a schedule tighter
        # than the sweep itself would queue runs faster than they can finish.
        sa.Column("interval_hours", sa.Integer, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False,
                  server_default=sa.text("true")),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("discovery_run.id", ondelete="SET NULL")),
        sa.Column("created_by", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("interval_hours >= 1",
                           name="ck_discovery_schedule_interval"),
        sa.CheckConstraint("cardinality(subnets) >= 1",
                           name="ck_discovery_schedule_subnets"),
    )
    # The scheduler's only question: what is due now.
    op.create_index("ix_discovery_schedule_due", "discovery_schedule",
                    ["next_run_at"], postgresql_where=sa.text("enabled"))

    # Which schedule queued a run, so a run can say "nightly" rather than just
    # appearing, and a schedule can show its own last result.
    op.add_column("discovery_run", sa.Column(
        "schedule_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("discovery_schedule.id", ondelete="SET NULL")))

    # ------------------------------------------------------ identity changes
    op.create_table(
        "discovery_identity_change",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("discovery_candidate.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("discovery_run.id", ondelete="SET NULL")),
        sa.Column("field", sa.Text, nullable=False),
        sa.Column("old_value", sa.Text),
        sa.Column("new_value", sa.Text),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_by", sa.Text),
    )
    # The page reads the OPEN changes for every candidate it lists.
    op.create_index("ix_discovery_change_open", "discovery_identity_change",
                    ["candidate_id"],
                    postgresql_where=sa.text("acknowledged_at IS NULL"))
    op.create_index("ix_discovery_change_run", "discovery_identity_change",
                    ["run_id"])


def downgrade() -> None:
    op.drop_table("discovery_identity_change")
    op.drop_column("discovery_run", "schedule_id")
    op.drop_table("discovery_schedule")
    for col in ("claimed_at", "changed", "gone", "appeared",
                "with_serial", "moved", "unknown", "known"):
        op.drop_column("discovery_run", col)
