"""Pools carry default credentials: a credential set per management network.

docs/26 Phase 4 planned "credential sets assignable to pools" and nothing
built them. Every endpoint pointed at its own credential - 894 v2c rows, one
per device, because the simulator's community is the device's IP. That is not
how an estate is run once it moves to SNMPv3: a site provisions one v3 user
across a device class (every PDU and UPS card on its BMS network), with keys
localised per device by engine ID, and the poller is given that one credential
for the network, with per-device exceptions.

collector_pool_credential maps (pool, protocol) to a credential. An endpoint's
own credential_id still wins; with none, the assignment serves its pool's
default for its protocol. Real foreign keys rather than a JSONB map on the
pool: a credential in use as a default must not be deletable out from under
the pools that rely on it.

Revision ID: 0097
Revises: 0096
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collector_pool_credential",
        sa.Column("pool_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("collector_pool.id", ondelete="CASCADE"), primary_key=True),
        # The endpoint protocol it applies to - text like device_endpoint's
        # protocol::text comparisons elsewhere, checked against the polled ones.
        sa.Column("protocol", sa.Text, primary_key=True),
        sa.Column("credential_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("credential.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_by", sa.Text),
        sa.CheckConstraint("protocol IN ('snmp', 'redfish', 'gnmi', 'modbus', 'bacnet')",
                           name="protocol_polled"),
    )
    op.create_index("ix_collector_pool_credential_credential",
                    "collector_pool_credential", ["credential_id"])


def downgrade() -> None:
    op.drop_index("ix_collector_pool_credential_credential", "collector_pool_credential")
    op.drop_table("collector_pool_credential")
