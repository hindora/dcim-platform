"""Record when each endpoint last produced telemetry.

Revision ID: 0011
Revises: 0010

An endpoint can be perfectly reachable and still silent: the poll succeeds, the
session authenticates, the device answers - and no measurement arrives. A hung
agent, a sensor that stopped updating, a mapping that no longer matches the
firmware's OIDs. Nothing in endpoint_state could tell that apart from healthy,
because every column there describes the POLL, not what the poll returned.

This cannot be derived from telemetry_sample: that table is keyed by device,
not endpoint, so a server whose BMC went silent still looks fresh on the
strength of its OS agent. The collector knows which endpoint produced each
sample and the ingest worker already has it in hand, so the honest place to
record it is here, next to the rest of that endpoint's liveness.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "endpoint_state",
        sa.Column("last_telemetry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_endpoint_state_last_telemetry", "endpoint_state", ["last_telemetry_at"],
        postgresql_where=sa.text("last_telemetry_at IS NOT NULL"),
    )

    # Start the clock for endpoints that already exist.
    #
    # NULL would mean "has never delivered telemetry", which of every existing
    # row would be true only in the useless sense that the column did not exist
    # until now. A staleness sweep reading that literally alarms the entire
    # fleet the moment this deploys - 1386 endpoints on this one - which is the
    # fastest way to teach people to ignore the alarm.
    #
    # Seeding to now() says "watching starts here" rather than claiming an
    # observation. It is self-correcting: an endpoint that is genuinely silent
    # crosses its grace period within minutes and alarms properly, and one that
    # is healthy overwrites this on its next batch.
    op.execute("UPDATE endpoint_state SET last_telemetry_at = now()")


def downgrade() -> None:
    op.drop_index("ix_endpoint_state_last_telemetry", table_name="endpoint_state")
    op.drop_column("endpoint_state", "last_telemetry_at")
