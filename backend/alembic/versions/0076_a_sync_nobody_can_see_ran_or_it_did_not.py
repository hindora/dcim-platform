"""When the estate was last read from the device plane, and what it changed.

The importer has always been a command somebody ran on a shell. That is fine for
a one-off seed and wrong for an operation the DCIM's own record depends on: after
it, devices appear, devices get decommissioned and endpoints are created, and the
only evidence any of it happened was a JSON blob on somebody's terminal.

"When did we last sync, and what came in" is an operator question, so the answer
belongs in the database rather than in the API process's memory. It is also what
makes the run safe to trigger from a button: a second one cannot start while the
first is going, because the row says so.

WHY A TABLE AND NOT AN IN-MEMORY JOB. The import takes about 90 seconds against
this estate, so it cannot be a synchronous request - and a job dict in the process
loses its history on every restart, cannot refuse a concurrent run started by
another worker, and answers none of the questions above. `discovery_run` already
set this pattern for the same reason.

Revision ID: 0076
Revises: 0075
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sim_import_run",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        # running | completed | failed. Text with a CHECK rather than an enum:
        # three values that will not grow, and an enum costs a migration to
        # extend (see 0043 for why that is more annoying than it sounds).
        sa.Column("status", sa.Text, nullable=False, server_default="running"),
        # Who pressed the button. A sync rewrites placement and endpoints across
        # the estate, so it is as attributable as any other privileged action.
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("source", sa.Text),
        # The ImportReport, verbatim. Stored whole rather than split into columns
        # because its shape is the importer's business and adding a counter there
        # should not need a migration here.
        sa.Column("report", sa.dialects.postgresql.JSONB),
        sa.Column("error", sa.Text),
        sa.CheckConstraint("status IN ('running','completed','failed')",
                           name="ck_sim_import_run_status"),
    )
    op.create_index("ix_sim_import_run_started", "sim_import_run",
                    [sa.text("started_at DESC")])
    # At most one run in flight, enforced by the database rather than by a check
    # in the handler: two imports writing the whole estate at once would
    # interleave their decommission sweeps, and each would see the other's
    # half-written devices as absent.
    op.execute("""
        CREATE UNIQUE INDEX ix_sim_import_run_one_at_a_time
            ON sim_import_run ((status))
         WHERE status = 'running'
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sim_import_run_one_at_a_time")
    op.drop_index("ix_sim_import_run_started", table_name="sim_import_run")
    op.drop_table("sim_import_run")
