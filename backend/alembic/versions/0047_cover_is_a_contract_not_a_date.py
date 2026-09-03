"""Support contracts, and what covers what.

Warranty is modelled as a CONTRACT that covers assets, not as a date on each
one. One contract covers many devices and renews as a unit; putting the date
only on the device means renewing two hundred rows and getting a hundred and
ninety-seven of them.

`device.warranty_expires` stays, as a cache, and this migration is where the
thing it caches finally exists. It holds the LATEST end date among the device's
active contracts - the date cover actually runs out. With cover to 2027 and to
2029 a device is covered until 2029; the earliest date is when the FIRST
contract lapses, which is a different question and not the one an asset list is
asked.

Numbered 0047 because phase 3 took 0046: alembic is a linear chain and
maintenance shipped first.

Revision ID: 0047
Revises: 0046
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "support_contract",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("supplier_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("supplier.id")),
        # The supplier's own number, which is what somebody quotes on the phone.
        sa.Column("reference", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        # NBD, 4h onsite, 24x7x4. Free text because every vendor names these
        # differently and an enum would be wrong within a quarter.
        sa.Column("service_level", sa.Text),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column("cost", sa.Numeric(12, 2)),
        sa.Column("currency", sa.CHAR(3)),
        sa.Column("auto_renew", sa.Boolean, nullable=False,
                  server_default=sa.text("false")),
        sa.Column("notes", sa.Text),
        sa.Column("attributes", sa.dialects.postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("end_date >= start_date", name="ck_support_contract_dates"),
        sa.CheckConstraint("kind IN ('warranty','support','maintenance')",
                           name="ck_support_contract_kind"),
        # A supplier does not issue the same reference twice. Scoped to the
        # supplier because two vendors absolutely will both use "C-1001".
        sa.UniqueConstraint("supplier_id", "reference",
                            name="uq_support_contract_reference"),
    )
    op.execute("""
        CREATE INDEX ix_support_contract_expiry ON support_contract (end_date)
    """)

    op.create_table(
        "device_support",
        sa.Column("device_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("device.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("contract_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("support_contract.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_device_support_contract", "device_support", ["contract_id"])

    # ---------------------------------------------------------------- tags

    op.create_table(
        "tag",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        # key/value rather than a flat label, so `env=prod` and `env=dev` are
        # one dimension with two values and the UI can offer a picker rather
        # than a text box that collects `Prod`, `prod` and `production`.
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("colour", sa.Text),
        sa.Column("description", sa.Text),
        sa.UniqueConstraint("key", "value", name="uq_tag_kv"),
    )

    op.create_table(
        "tag_assignment",
        sa.Column("tag_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True),
        # Polymorphic like connection terminations, and for the same reason:
        # tags apply to devices, racks and rooms, and three near-identical join
        # tables is worse than one column validated in the repository.
        sa.Column("object_type", sa.Text, nullable=False, primary_key=True),
        sa.Column("object_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  nullable=False, primary_key=True),
        sa.Column("assigned_by", sa.Text, nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("object_type IN ('device','rack','room')",
                           name="ck_tag_assignment_object_type"),
    )
    op.create_index("ix_tag_assignment_object", "tag_assignment",
                    ["object_type", "object_id"])


def downgrade() -> None:
    op.drop_index("ix_tag_assignment_object", table_name="tag_assignment")
    op.drop_table("tag_assignment")
    op.drop_table("tag")
    op.drop_index("ix_device_support_contract", table_name="device_support")
    op.drop_table("device_support")
    op.execute("DROP INDEX IF EXISTS ix_support_contract_expiry")
    op.drop_table("support_contract")
    # warranty_expires is a cache of what just went away, so it has to go back
    # to NULL rather than keep a number nothing can now explain.
    op.execute("UPDATE device SET warranty_expires = NULL")
