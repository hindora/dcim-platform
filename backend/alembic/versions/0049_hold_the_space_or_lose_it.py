"""Capacity reservations: holding rack units and power that nothing occupies yet.

docs/19 B6. `/capacity` measures utilisation well and never commits any of it, so
two teams read the same free-U number and both act on it. The conflict then
surfaces at install time, with hardware on a trolley.

THE U RANGE IS ENFORCED BY THE CONSTRAINT THAT ALREADY WORKS. PostgreSQL
exclusion constraints cannot span tables, so `device_u_no_overlap` cannot be
extended to cover a second table - and a cross-table trigger doing the same job
needs explicit locking to be correct under concurrency, which is easy to get
subtly wrong and impossible to notice until two installs collide.

So a reservation that names a U range creates a `device` row with
`lifecycle = 'planned'` instead, and `device_u_no_overlap` rejects the overlap
with no new code. The rack elevation renders it for free, and fulfilling the
reservation is an UPDATE of that row rather than a create-and-delete - so the
machine that eventually lands keeps the reservation's own history.

This table then holds what the device row cannot: the power and cooling being
held, the project holding it, and the expiry.

`expires_at` is NOT NULL on purpose. The failure mode of this feature everywhere
it exists is a rack held for a project cancelled two years ago that nobody
released.

Revision ID: 0049
Revises: 0048
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capacity_reservation",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("rack_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("rack.id", ondelete="CASCADE")),
        sa.Column("room_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("room.id", ondelete="CASCADE")),
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("owner_group", sa.Text),
        sa.Column("u_start", sa.Integer),
        sa.Column("u_height", sa.Integer),
        sa.Column("power_kw", sa.Numeric(8, 2)),
        sa.Column("cool_kw", sa.Numeric(8, 2)),
        sa.Column("needed_by", sa.Date),
        sa.Column("expires_at", sa.Date, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="held"),
        # The `planned` device standing in for the U range, so
        # device_u_no_overlap does the enforcing. NULL for a room-level hold of
        # power with no specific rack units.
        sa.Column("placeholder_device_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("device.id", ondelete="SET NULL")),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("notes", sa.Text),
        # A reservation has to be somewhere. Holding capacity in the abstract is
        # a spreadsheet row, not a commitment against a rack somebody else wants.
        sa.CheckConstraint("rack_id IS NOT NULL OR room_id IS NOT NULL",
                           name="ck_reservation_scope"),
        sa.CheckConstraint(
            "(u_start IS NULL AND u_height IS NULL) OR "
            "(u_start IS NOT NULL AND u_height >= 1)",
            name="ck_reservation_u"),
        # A U range without a rack cannot be enforced by anything.
        sa.CheckConstraint("u_start IS NULL OR rack_id IS NOT NULL",
                           name="ck_reservation_u_needs_rack"),
        sa.CheckConstraint(
            "status IN ('held','fulfilled','released','expired')",
            name="ck_reservation_status"),
    )
    op.execute("""
        CREATE INDEX ix_reservation_open ON capacity_reservation (expires_at)
        WHERE status = 'held'
    """)
    op.create_index("ix_reservation_rack", "capacity_reservation", ["rack_id"])
    op.create_index("ix_reservation_project", "capacity_reservation", ["project"])


def downgrade() -> None:
    # The placeholder devices go with the reservations that own them. Left
    # behind they are `planned` rows nobody can explain, occupying rack units
    # against a commitment that no longer exists anywhere.
    op.execute("""
        DELETE FROM device
        WHERE lifecycle = 'planned'
          AND id IN (SELECT placeholder_device_id FROM capacity_reservation
                     WHERE placeholder_device_id IS NOT NULL)
    """)
    op.drop_index("ix_reservation_project", table_name="capacity_reservation")
    op.drop_index("ix_reservation_rack", table_name="capacity_reservation")
    op.execute("DROP INDEX IF EXISTS ix_reservation_open")
    op.drop_table("capacity_reservation")
