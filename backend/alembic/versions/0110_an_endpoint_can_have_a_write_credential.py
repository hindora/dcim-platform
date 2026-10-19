"""An endpoint can have a credential just for writing.

A polling credential is read-only on purpose: an SNMP community or v3 user
created for monitoring is given a view that cannot change anything, and the
site that gives a monitor write access has made every dashboard a way to switch
a PDU. Real sites keep a separate write community or a v3 user with a write
view, held by the few that need it. A device action - an outlet switched over
SNMP - therefore needs its own credential, and an endpoint without one cannot
be written to, which is the right default.

`write_credential_id` is that credential, delivered to the collector that owns
the endpoint sealed to its key exactly as the polling credential is, and used
for nothing but device actions. Null means no writes. A Redfish BMC needs no
second one: its one account either may reset the machine or it may not.

Revision ID: 0110
Revises: 0109
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "0110"
down_revision = "0109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("device_endpoint", sa.Column(
        "write_credential_id", pg.UUID(as_uuid=True),
        sa.ForeignKey("credential.id", ondelete="SET NULL")))


def downgrade() -> None:
    op.drop_column("device_endpoint", "write_credential_id")
