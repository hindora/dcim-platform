"""The command queue carries device actions.

`collector_command` was built for one kind, a collector upgrade, and the
table's CHECK says so. A device action - switch a PDU outlet, restart a server
through its BMC - is a command for the collector that owns the device's
endpoint: the platform does not hold the device's credentials in a form it can
use (they are sealed to the collector) and does not sit on the management
network, so the write has to be made by the process that does. That is what the
queue is for; this widens the CHECK so it can carry one.

Revision ID: 0109
Revises: 0108
"""

from __future__ import annotations

from alembic import op

revision = "0109"
down_revision = "0108"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("collector_command_kind", "collector_command", type_="check")
    op.create_check_constraint("collector_command_kind", "collector_command",
                               "kind IN ('upgrade', 'device_action')")


def downgrade() -> None:
    # An action row would violate the narrower CHECK, and an action is a
    # moment-in-time request, not a record anything depends on.
    op.execute("DELETE FROM collector_command WHERE kind = 'device_action'")
    op.drop_constraint("collector_command_kind", "collector_command", type_="check")
    op.create_check_constraint("collector_command_kind", "collector_command",
                               "kind IN ('upgrade')")
