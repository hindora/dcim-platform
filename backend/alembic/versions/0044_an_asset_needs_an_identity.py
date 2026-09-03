"""Supplier, the commercial columns on device, and the identity constraints.

This is the migration docs/19 B2 is about. `serial_number` and `asset_tag` have
existed on `device` since the baseline and were NULL on all 664 rows, with no
unique index on either - so discovery's reconciliation, which matches on serial
first, could never match anything and every sweep produced duplicates for an
operator to resolve by hand.

Two things happen here. The commercial fields an asset record needs get columns,
and the two identity columns get partial unique indexes.

ORDER OF OPERATIONS MATTERS AND IS NOT OPTIONAL. Run the importer first, with a
simulator export that carries serials, and check for duplicates BEFORE applying
this. A unique index build that fails on real data is a loud, recoverable error
and that is exactly why it is done in this order rather than by declaring the
column UNIQUE from the start and finding out during a release.

Revision ID: 0044
Revises: 0043
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Who you BOUGHT from and who SUPPORTS it are not who MANUFACTURED it, and
    # they are routinely three different companies. `vendor` is the
    # manufacturer - it carries enterprise_oid for trap mapping, which is a hint
    # about what it is for. A reseller has no enterprise OID and never appears
    # in a trap.
    op.create_table(
        "supplier",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("account_ref", sa.Text),
        sa.Column("contact_name", sa.Text),
        sa.Column("contact_email", sa.Text),
        sa.Column("contact_phone", sa.Text),
        sa.Column("notes", sa.Text),
        sa.Column("attributes", sa.dialects.postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    for column in (
        sa.Column("supplier_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("supplier.id")),
        sa.Column("purchase_date", sa.Date),
        sa.Column("purchase_order", sa.Text),
        sa.Column("purchase_cost", sa.Numeric(12, 2)),
        sa.Column("currency", sa.CHAR(3)),
        sa.Column("install_date", sa.Date),
        # A CACHE. The authoritative record is support_contract (migration
        # 0047); this is the LATEST active covering expiry - the date cover
        # actually runs out - maintained by the code that writes the link. It exists because the asset
        # list sorts and filters by expiry on every page load and a three-table
        # join per keystroke is not that. Nothing else may write it.
        sa.Column("warranty_expires", sa.Date),
        sa.Column("eol_date", sa.Date),
        sa.Column("eos_date", sa.Date),
        sa.Column("owner_group", sa.Text),
        sa.Column("cost_centre", sa.Text),
        sa.Column("notes", sa.Text),
    ):
        op.add_column("device", column)

    # Partial, not a plain UNIQUE. NULLs are not comparable in Postgres so a
    # plain unique constraint would permit them anyway - being explicit about
    # WHERE documents the intent, which is "two assets may both be unidentified,
    # but no two may claim the same identity".
    op.execute("""
        CREATE UNIQUE INDEX ix_device_serial_unique ON device (serial_number)
        WHERE serial_number IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX ix_device_asset_tag_unique ON device (asset_tag)
        WHERE asset_tag IS NOT NULL
    """)
    # Partial because an asset with no warranty date is not a row the
    # "expiring soon" query ever wants to read.
    op.execute("""
        CREATE INDEX ix_device_warranty_expires ON device (warranty_expires)
        WHERE warranty_expires IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_device_warranty_expires")
    op.execute("DROP INDEX IF EXISTS ix_device_asset_tag_unique")
    op.execute("DROP INDEX IF EXISTS ix_device_serial_unique")
    for name in ("notes", "cost_centre", "owner_group", "eos_date", "eol_date",
                 "warranty_expires", "install_date", "currency",
                 "purchase_cost", "purchase_order", "purchase_date",
                 "supplier_id"):
        op.drop_column("device", name)
    op.drop_table("supplier")
