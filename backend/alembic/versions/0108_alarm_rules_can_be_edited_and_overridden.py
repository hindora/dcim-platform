"""Alarm rules can be edited, added and overridden per device.

Rules were seeded by migrations and could only be switched on or off: a
threshold that was wrong for a site meant a migration. This adds what an
operator needs to own their monitoring.

`alarm_rule_override` is one device's exception to one rule, which is how
every DCIM that edits thresholds does it (estate policy by device type, with a
per-device override that takes precedence). A null column inherits the rule's
value, so an override can move only the threshold, or only mute the device:
`enabled = false` is "do not raise this alarm on this device", which is the
mute. Overrides are deleted with their device or rule.

`alarm_rule.custom` marks a rule an operator wrote, so only those can be
deleted and the seeded policy cannot be removed by accident; `edited_by` and
`edited_at` say who last changed a rule and when, beside the audit row.

Revision ID: 0108
Revises: 0107
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("alarm_rule", sa.Column(
        "custom", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("alarm_rule", sa.Column("edited_by", sa.Text()))
    op.add_column("alarm_rule", sa.Column("edited_at", sa.DateTime(timezone=True)))

    op.create_table(
        "alarm_rule_override",
        sa.Column("rule_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("alarm_rule.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        # null = inherit the rule's value
        sa.Column("threshold", sa.Numeric()),
        sa.Column("clear_threshold", sa.Numeric()),
        sa.Column("dwell_samples", sa.Integer()),
        sa.Column("dwell_seconds", sa.Integer()),
        sa.Column("clear_dwell_samples", sa.Integer()),
        sa.Column("severity", pg.ENUM(name="severity_t", create_type=False)),
        # false = mute this rule on this device; null = inherit
        sa.Column("enabled", sa.Boolean()),
        sa.Column("reason", sa.Text()),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("rule_id", "device_id"),
    )
    op.create_index("ix_alarm_rule_override_device", "alarm_rule_override", ["device_id"])


def downgrade() -> None:
    op.drop_index("ix_alarm_rule_override_device", table_name="alarm_rule_override")
    op.drop_table("alarm_rule_override")
    op.drop_column("alarm_rule", "edited_at")
    op.drop_column("alarm_rule", "edited_by")
    op.drop_column("alarm_rule", "custom")
