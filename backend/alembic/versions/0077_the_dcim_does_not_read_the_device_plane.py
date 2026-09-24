"""Drop `sim_import_run`: the thing it recorded should not have existed.

0076 added a table behind a "sync" button that logged into the simulator's REST
API and read its topology export. That is backend to backend - one product
reading another's database through its front door - and it is the wrong shape at
the root, not a feature with a bug in it. The DCIM must not know what is
generating its telemetry.

Finding hardware somebody racked is DISCOVERY's job: a sweep over SNMP/Redfish/
gNMI on the management network, run by the collector because the collector is
what sits on that network. That already exists (migration 0012,
`collector/internal/discovery`). What was missing was a screen for it, which is
what replaces the button.

The importer keeps exactly one job - seeding a development estate from a fixture -
and that belongs on a command line.

Dropped rather than left in place. An unused table is a standing invitation to
wire it back up, and this one would bring the coupling with it.

Revision ID: 0077
Revises: 0076
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sim_import_run_one_at_a_time")
    op.execute("DROP INDEX IF EXISTS ix_sim_import_run_started")
    op.execute("DROP TABLE IF EXISTS sim_import_run")


def downgrade() -> None:
    """Recreate it empty, so a rollback to 0076 finds the schema it expects.

    The rows are not recoverable and are not worth recovering: each recorded one
    read of a device plane the DCIM should not have been reading.
    """
    op.create_table(
        "sim_import_run",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.Text, nullable=False, server_default="running"),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("source", sa.Text),
        sa.Column("report", sa.dialects.postgresql.JSONB),
        sa.Column("error", sa.Text),
        sa.CheckConstraint("status IN ('running','completed','failed')",
                           name="ck_sim_import_run_status"),
    )
    op.create_index("ix_sim_import_run_started", "sim_import_run",
                    [sa.text("started_at DESC")])
    op.execute("""
        CREATE UNIQUE INDEX ix_sim_import_run_one_at_a_time
            ON sim_import_run ((status))
         WHERE status = 'running'
    """)
