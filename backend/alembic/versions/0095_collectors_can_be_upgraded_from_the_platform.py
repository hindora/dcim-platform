"""Collectors can be upgraded from the platform: commands, releases, rollouts.

docs/26 Phase 7. Three tables, one mechanism:

- collector_command - work the platform hands one collector, delivered on a
  long-poll (GET /collector/commands) so it arrives in a second rather than on
  the next poll of anything. Kept as history, not overwritten: "who told
  col-dc1-bms to upgrade, when, and what happened" is the audit question a
  failed rollout raises. Only `upgrade` exists today; the CHECK widens with
  the next kind.
- collector_release - a signed collector build. The platform stores and
  serves the artefact, but cannot sign one: the Ed25519 private key lives
  offline with whoever builds releases (scripts/publish_release.py), and each
  collector trusts only the public keys in its own config. A compromised
  platform can therefore withhold an upgrade, never push a binary of its own.
- collector_rollout - one target version over chosen pools, upgraded one
  member per pool at a time, each waiting for the previous to come back
  healthy on the new version (the orchestrator in app/services/rollout.py).

Revision ID: 0095
Revises: 0094
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None

COMMAND_KINDS = ("upgrade",)
COMMAND_STATES = ("pending", "delivered", "succeeded", "failed", "expired", "cancelled")
ROLLOUT_STATES = ("running", "succeeded", "failed", "cancelled")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.create_table(
        "collector_release",
        sa.Column("version", sa.Text, primary_key=True),
        sa.Column("sha256", sa.Text, nullable=False),
        # Ed25519 over the 32 raw bytes of the sha256, base64.
        sa.Column("signature", sa.Text, nullable=False),
        # Which trusted key signed it, so a collector can say "not signed by
        # a key I trust" rather than just "bad signature".
        sa.Column("key_id", sa.Text, nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_by", sa.Text),
    )
    op.create_table(
        "collector_rollout",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("version", sa.Text, sa.ForeignKey("collector_release.version"),
                  nullable=False),
        sa.Column("pool_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
                  nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="running"),
        sa.Column("detail", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_by", sa.Text),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(_in("state", ROLLOUT_STATES), name="collector_rollout_state"),
    )
    op.create_table(
        "collector_command",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("collector_id", sa.Text,
                  sa.ForeignKey("collector_instance.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("state", sa.Text, nullable=False, server_default="pending"),
        sa.Column("rollout_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("collector_rollout.id", ondelete="SET NULL")),
        sa.Column("result", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_by", sa.Text),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(_in("kind", COMMAND_KINDS), name="collector_command_kind"),
        sa.CheckConstraint(_in("state", COMMAND_STATES), name="collector_command_state"),
    )
    op.create_index("ix_collector_command_open", "collector_command",
                    ["collector_id", "state"])


def downgrade() -> None:
    op.drop_index("ix_collector_command_open", table_name="collector_command")
    op.drop_table("collector_command")
    op.drop_table("collector_rollout")
    op.drop_table("collector_release")
