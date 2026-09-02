"""Maintenance windows, and what they do to alarms.

The state existed and nothing read it. `AdminState.MAINTENANCE` has been a
column on device, interface and connection since the baseline, and the alarm
engine never consulted it - so planned work generated exactly the signals the
engine is built to escalate. A server powered down reads as unreachable, a CRAH
isolated for a filter change reads as cooling lost, a PDU on maintenance bypass
reads as redundancy lost. If those page, operators learn to ignore the console
during work windows, and it then stops working for the unplanned case too -
which is the expensive failure, not the noise.

SHELVE, DO NOT SUPPRESS. An alarm on a device inside an active window is raised
and stored exactly as normal, then marked, and excluded from the active list,
the roll-ups and the notification path. Never-raising is cheaper and wrong: the
question asked after every work window is "did anything ELSE break while we were
in there", and it cannot be answered from alarms that were never written.

This is also what the tools operators already use do. Zabbix distinguishes
maintenance "with data collection" - problems still detected, marked suppressed,
hidden from the dashboard by default - from "no data collection", and the first
is the one everybody runs. `is_symptom` in this schema is the same shape
already: a real alarm, stored, held out of the primary view.

Numbered 0046 rather than the 0047 in docs/20: alembic is a linear chain, phase
3 lands before phase 4, and support contracts take 0047 instead.

Revision ID: 0046
Revises: 0045
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenance_window",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("change_ref", sa.Text),
        sa.Column("kind", sa.Text, nullable=False, server_default="planned"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        # scheduled -> active -> completed | cancelled. A COLUMN, advanced by a
        # ticker, not a predicate over now(): the ingest worker and the API have
        # to agree about whether a window is running, and two processes reading
        # their own clocks do not. A window that is "active" to the worker and
        # "scheduled" to the API shelves an alarm the operator can still see.
        sa.Column("status", sa.Text, nullable=False, server_default="scheduled"),
        sa.Column("suppress", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("ends_at > starts_at", name="ck_maintenance_window_dates"),
        sa.CheckConstraint("status IN ('scheduled','active','completed','cancelled')",
                           name="ck_maintenance_window_status"),
        sa.CheckConstraint("kind IN ('planned','emergency')",
                           name="ck_maintenance_window_kind"),
    )
    # The ticker's own query: which windows are due to start or finish.
    op.execute("""
        CREATE INDEX ix_mw_pending ON maintenance_window (starts_at, ends_at)
        WHERE status IN ('scheduled', 'active')
    """)

    op.create_table(
        "maintenance_target",
        sa.Column("window_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("maintenance_window.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("device_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("device.id", ondelete="CASCADE"),
                  primary_key=True),
    )
    op.create_index("ix_maintenance_target_device", "maintenance_target",
                    ["device_id"])

    op.create_table(
        "maintenance_record",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("device_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        # Nullable, and that is the point: emergency work has a record and no
        # window, and a window can end with nothing done.
        sa.Column("window_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("maintenance_window.id", ondelete="SET NULL")),
        sa.Column("performed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("performed_by", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("detail", sa.Text),
        # Free-form until migration 0049 gives parts a table of their own. Then
        # this becomes the audit trail behind the stock movements.
        sa.Column("parts_used", sa.dialects.postgresql.JSONB, nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("attributes", sa.dialects.postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_maintenance_record_device", "maintenance_record",
                    ["device_id", sa.text("performed_at DESC")])

    # The mark. Nullable FK rather than a boolean, so the window that shelved an
    # alarm is recoverable - "3 alarms shelved" on a window page is how you find
    # out the window was scoped too widely, and a boolean cannot say by what.
    op.add_column("alarm", sa.Column(
        "shelved_by_window", sa.dialects.postgresql.UUID(as_uuid=True),
        sa.ForeignKey("maintenance_window.id", ondelete="SET NULL")))
    op.execute("""
        CREATE INDEX ix_alarm_shelved ON alarm (shelved_by_window)
        WHERE shelved_by_window IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_alarm_shelved")
    op.drop_column("alarm", "shelved_by_window")
    op.drop_index("ix_maintenance_record_device", table_name="maintenance_record")
    op.drop_table("maintenance_record")
    op.drop_index("ix_maintenance_target_device", table_name="maintenance_target")
    op.drop_table("maintenance_target")
    op.execute("DROP INDEX IF EXISTS ix_mw_pending")
    op.drop_table("maintenance_window")
