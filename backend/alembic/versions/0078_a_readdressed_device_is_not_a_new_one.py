"""Discovery matches on the serial, not only on the address.

A sweep matched a responder to inventory by its address and nothing else, so a
device that had been re-addressed came back as brand new - and promoting it
created a SECOND record for one physical box. That is the failure an audit exists
to prevent, produced by the audit.

The serial is the right key and the estate already carries it:
`ix_device_serial_unique` has existed since 0044, and every device in this
deployment has one. What was missing is that the sweep never read it, so there was
nothing to match against. The candidate queue even carried a banner about serial
matching being unavailable "because no asset carries a serial" - which stopped
being true, so the banner went away and quietly implied the matching worked.

Nullable, because plenty of real gear does not answer entPhysicalSerialNum and a
sweep that demanded one would report less than it found. Address matching stays as
the fallback; a serial only ever makes a match MORE certain.

Revision ID: 0078
Revises: 0077
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("discovery_candidate", sa.Column("serial", sa.Text))
    # Partial: the lookup only ever asks about rows that have one, and a sweep of
    # a /24 writes 250 rows at a time.
    op.execute("""
        CREATE INDEX ix_discovery_candidate_serial
            ON discovery_candidate (serial)
         WHERE serial IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_discovery_candidate_serial")
    op.drop_column("discovery_candidate", "serial")
