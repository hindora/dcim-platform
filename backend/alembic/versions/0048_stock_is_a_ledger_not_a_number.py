"""Consumable parts, where they are kept, and every movement between.

docs/19 B3: a serialised asset and a count of consumables are two different
things and the plan held both in one model. An asset has identity, a location, a
lifecycle and telemetry; a consumable has a count at a place and none of those.
Put them in one table and every row is half NULL, `quantity` is meaningless for
the individuals and `rack_id` is meaningless for the stock.

The rule, stated once so it is not re-litigated per item: **if the individual is
tracked it is a device; if only the count is tracked it is a part.** A spare
SERVER has a serial and will be racked and polled - it is a `device` with
`lifecycle = 'in_stock'`. A spare fan is a part and never a device.

`on_hand` is DERIVED from stock_movement, and there is deliberately no way to
set it directly. A stock figure somebody can overwrite is a spreadsheet: the
question "we had four last week, where did they go" has no answer, and every
discrepancy becomes one person's memory against a number. Correcting a count is
posting an adjustment with a note, which leaves a record of the correction.

Revision ID: 0048
Revises: 0047
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "part",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        # The manufacturer's part number, which is what somebody reads off the
        # box and types into a supplier's site.
        sa.Column("sku", sa.Text, nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("vendor_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("vendor.id")),
        # Which device types this part fits. ADVISORY - used to offer the right
        # parts on a device's maintenance form, never enforced. Cross-compatible
        # parts are the norm and a hard constraint here would be wrong within a
        # month of the first unusual rebuild.
        sa.Column("fits_types", sa.dialects.postgresql.ARRAY(sa.Text),
                  nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("unit_cost", sa.Numeric(12, 2)),
        sa.Column("currency", sa.CHAR(3)),
        sa.Column("attributes", sa.dialects.postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "category IN ('psu','fan','memory','disk','optic','cable','controller',"
            "'battery','filter','other')", name="ck_part_category"),
    )
    op.create_index("ix_part_category", "part", ["category"])

    op.create_table(
        "store",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text, nullable=False),
        # A store is a PLACE, not a rack. It may be a room in a datacenter or an
        # offsite depot, so both links are optional.
        sa.Column("datacenter_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("datacenter.id")),
        sa.Column("room_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("room.id")),
        sa.Column("location_note", sa.Text),
        sa.UniqueConstraint("datacenter_id", "name", name="uq_store_name"),
    )

    op.create_table(
        "part_stock",
        sa.Column("part_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("part.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("store_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("store.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("on_hand", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reserved", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reorder_at", sa.Integer),
        sa.Column("reorder_to", sa.Integer),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("on_hand >= 0 AND reserved >= 0", name="ck_part_stock_nonneg"),
        # Reserving more than exists is how a rebuild is planned against parts
        # that are already promised elsewhere.
        sa.CheckConstraint("reserved <= on_hand", name="ck_part_stock_reserved"),
    )
    op.execute("""
        CREATE INDEX ix_part_stock_low ON part_stock (part_id)
        WHERE reorder_at IS NOT NULL AND on_hand <= reorder_at
    """)

    op.create_table(
        "stock_movement",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("part_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("part.id", ondelete="CASCADE"), nullable=False),
        sa.Column("store_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("store.id", ondelete="CASCADE"), nullable=False),
        # Signed: positive is a receipt, negative is consumption. One column
        # rather than a direction plus a magnitude, so `SUM(delta)` is the
        # balance and cannot disagree with itself.
        sa.Column("delta", sa.Integer, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        # Set when a part was consumed ON something, which is what turns the
        # ledger into a maintenance history rather than a stock report.
        sa.Column("device_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("device.id", ondelete="SET NULL")),
        sa.Column("record_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("maintenance_record.id", ondelete="SET NULL")),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("note", sa.Text),
        sa.CheckConstraint("delta <> 0", name="ck_stock_movement_nonzero"),
        sa.CheckConstraint(
            "reason IN ('receipt','consumed','adjustment','rma','transfer')",
            name="ck_stock_movement_reason"),
        # An adjustment is somebody overriding the ledger with a physical count.
        # It has to say why, or it is the silent overwrite this table exists to
        # prevent, wearing a different name.
        sa.CheckConstraint("reason <> 'adjustment' OR note IS NOT NULL",
                           name="ck_stock_movement_adjustment_note"),
    )
    op.create_index("ix_stock_movement_part", "stock_movement",
                    ["part_id", sa.text("ts DESC")])
    op.execute("""
        CREATE INDEX ix_stock_movement_device ON stock_movement (device_id)
        WHERE device_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_stock_movement_device")
    op.drop_index("ix_stock_movement_part", table_name="stock_movement")
    op.drop_table("stock_movement")
    op.execute("DROP INDEX IF EXISTS ix_part_stock_low")
    op.drop_table("part_stock")
    op.drop_table("store")
    op.drop_index("ix_part_category", table_name="part")
    op.drop_table("part")
