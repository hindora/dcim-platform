"""A device nothing polls is not monitored, not unknown.

`device_comm_status(device_id, status)` is the one place the estate's pages
read a device's communication state from. A device with no endpoint at all -
a remote power panel with no monitoring card, read through the branch meter
clamped on its conductors - has no communication state to report, and calling
that UNKNOWN filed sixteen healthy, metered panels beside the devices that
were configured for polling and never answered. Commercial DCIM keeps the two
apart the same way: an asset without a monitoring connection is "not
monitored"; "unknown" is a polling target that has not been heard from.

The endpoint test wins over a stored status: a device whose endpoints were
all removed keeps whatever its last rollup wrote in device_state, and that
row says nothing about now.

Revision ID: 0120
Revises: 0119
"""

from __future__ import annotations

from alembic import op

revision = "0120"
down_revision = "0119"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION device_comm_status(dev uuid, st text)
        RETURNS text
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$
            SELECT CASE
                WHEN NOT EXISTS (SELECT 1 FROM device_endpoint e
                                  WHERE e.device_id = dev)
                    THEN 'NOT_MONITORED'
                ELSE COALESCE(st, 'UNKNOWN')
            END
        $$
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS device_comm_status(uuid, text)")
