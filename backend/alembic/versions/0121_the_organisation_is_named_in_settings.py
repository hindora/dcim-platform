"""The organisation's name is set in Settings, not only in the server's env file.

Until now the name on the Home headline, the login card and the browser tab came
from DCIM_ORG_NAME alone, so renaming the estate meant editing deploy/.env and
restarting the API. Commercial DCIM keeps this in the application's admin
settings, where an administrator changes it and the audit log records who did.

One row, enforced: `id` is pinned to 1. A NULL name (or no row at all) means
"not set here", and the API falls back to DCIM_ORG_NAME, then to the product
name - so an install that already sets the env var reads exactly as before until
somebody saves a name in Settings.

Revision ID: 0121
Revises: 0120
"""

from __future__ import annotations

from alembic import op

revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS instance_setting (
            id          smallint PRIMARY KEY DEFAULT 1,
            org_name    text,
            updated_at  timestamptz NOT NULL DEFAULT now(),
            updated_by  text,
            CONSTRAINT instance_setting_single_row CHECK (id = 1),
            CONSTRAINT instance_setting_org_name_shape CHECK (
                org_name IS NULL
                OR (char_length(org_name) BETWEEN 1 AND 80
                    AND org_name = btrim(org_name))
            )
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS instance_setting")
