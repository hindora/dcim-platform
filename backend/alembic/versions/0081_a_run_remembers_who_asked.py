"""A run remembers who asked for it, after the schedule that asked is gone.

discovery_run.schedule_id is ON DELETE SET NULL, and it has to be - a run must
outlive the schedule that queued it. But it was also the ONLY record that a
schedule queued it, so deleting a schedule rewrote its runs' history into sweeps
somebody ran by hand. Found live: a scheduled run lost its "scheduled" tag the
moment its schedule was deleted.

`trigger` says how the run started ('manual' or 'schedule') and `schedule_label`
keeps what the schedule was called when it fired. Both are the run's own facts,
written once, and neither depends on the schedule still existing.

Revision ID: 0081
Revises: 0080
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("discovery_run", sa.Column(
        "trigger", sa.Text, nullable=False, server_default="manual"))
    op.create_check_constraint(
        "ck_discovery_run_trigger", "discovery_run",
        "trigger IN ('manual', 'schedule')")
    op.add_column("discovery_run", sa.Column("schedule_label", sa.Text))
    # Runs a schedule queued before this migration, while their schedule still
    # exists to say so. Those whose schedule was already deleted cannot be told
    # apart from manual ones any more - which is the loss this fixes.
    op.execute("""
        UPDATE discovery_run r
           SET trigger = 'schedule',
               schedule_label = COALESCE(s.name, array_to_string(s.subnets, ', '))
          FROM discovery_schedule s
         WHERE s.id = r.schedule_id
    """)


def downgrade() -> None:
    op.drop_column("discovery_run", "schedule_label")
    op.drop_constraint("ck_discovery_run_trigger", "discovery_run", type_="check")
    op.drop_column("discovery_run", "trigger")
