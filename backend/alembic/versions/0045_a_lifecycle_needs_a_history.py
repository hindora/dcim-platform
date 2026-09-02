"""Lifecycle transitions, recorded with who and why.

`commissioned_at` and `decommissioned_at` are two timestamps on `device`. They
cannot answer "who moved this to maintenance on the 14th and why", and they are
overwritten by the next transition, so the answer is gone rather than wrong.

This does NOT replace `audit_log`, and the difference is worth stating because
the instinct is to collapse them. `audit_log` records that a field changed,
generically, for compliance, with credential scrubbing on the way in - it is
evidence. This records a business event somebody asked for, with a reason and a
change reference, on an index that answers "show me this asset's history" in one
scan rather than a JSONB predicate over an append-only table. Both are written
on a transition. Neither is derivable from the other.

Revision ID: 0045
Revises: 0044
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_lifecycle_event",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("device_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        # NULL on the first event: a device that appeared already in service came
        # from nowhere, and inventing a previous state would be a fact we made up.
        sa.Column("from_state",
                  sa.dialects.postgresql.ENUM(name="lifecycle_t", create_type=False)),
        sa.Column("to_state",
                  sa.dialects.postgresql.ENUM(name="lifecycle_t", create_type=False),
                  nullable=False),
        # The field a change board actually asks for, and the one audit_log
        # cannot hold because a generic before/after has nowhere to put it.
        sa.Column("reason", sa.Text),
        sa.Column("change_ref", sa.Text),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("attributes", sa.dialects.postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_dle_device_ts", "device_lifecycle_event",
                    ["device_id", sa.text("ts DESC")])
    op.create_index("ix_dle_ts", "device_lifecycle_event", [sa.text("ts DESC")])

    # Backfill from the two timestamps that were carrying this alone. They are
    # all the history there is, so this is the honest maximum - actor 'import'
    # rather than a person, because nobody knows who did it and guessing would
    # put a name against an action they may not have taken.
    op.execute("""
        INSERT INTO device_lifecycle_event (device_id, from_state, to_state,
                                            reason, actor, ts)
        SELECT id, NULL, 'in_service',
               'backfilled from commissioned_at', 'import', commissioned_at
        FROM device
        WHERE commissioned_at IS NOT NULL
    """)
    op.execute("""
        INSERT INTO device_lifecycle_event (device_id, from_state, to_state,
                                            reason, actor, ts)
        SELECT id, 'in_service', 'decommissioned',
               'backfilled from decommissioned_at', 'import', decommissioned_at
        FROM device
        WHERE decommissioned_at IS NOT NULL
    """)


def downgrade() -> None:
    op.drop_index("ix_dle_ts", table_name="device_lifecycle_event")
    op.drop_index("ix_dle_device_ts", table_name="device_lifecycle_event")
    op.drop_table("device_lifecycle_event")
