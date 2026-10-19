"""A device can be edited by hand, and a re-import leaves the edit alone.

Until now the device record came only from the importer: the one write route
on a device was the endpoint's connection settings, and a name, serial, model
or address could be corrected only by changing the simulator export and
re-importing. A DCIM is the system of record for its inventory, so an
operator has to be able to say what a device IS.

That has a catch the importer already solved once for geometry
(geometry_source = 'manual'): an upsert keyed on external_id puts the
export's value back on every run, silently undoing the edit. `locked_fields`
names the columns a person has set, and the importer's upsert keeps the
stored value for any of them. Clearing a name from the list hands the field
back to the importer.

`priority` is how much a device matters when something is wrong with it:
high, normal or low. It is an operator's statement, not a measurement, so it
has no importer default beyond 'normal'.

Revision ID: 0107
Revises: 0106
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("device", sa.Column(
        "locked_fields", sa.dialects.postgresql.ARRAY(sa.Text()),
        nullable=False, server_default=sa.text("'{}'::text[]")))
    op.add_column("device", sa.Column(
        "priority", sa.Text(), nullable=False, server_default="normal"))
    op.create_check_constraint("priority_known", "device",
                               "priority IN ('high', 'normal', 'low')")


def downgrade() -> None:
    op.drop_constraint("priority_known", "device", type_="check")
    op.drop_column("device", "priority")
    op.drop_column("device", "locked_fields")
