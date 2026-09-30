"""protocol_t gains 'provider', and nothing else in this migration.

docs/26 Phase 10: a colo tenant's facility telemetry - cabinet power and
environmentals from the provider's own API (Equinix Smart View / API Plus
and similar) rather than from anything this platform speaks to directly.
Modelled as just another endpoint protocol, so ownership, staleness and
collector-down semantics (docs/26 Phase 0) apply to it completely unchanged -
a provider-sourced endpoint that stops updating goes OFFLINE and pages the
same as an SNMP one that stops answering, with no separate code path to keep
correct.

Same PostgreSQL 12+ rule as 0043: ADD VALUE cannot be used in the same
transaction that references the new label, so this migration adds the label
alone and nothing that would read or write 'provider' follows in the same
revision.

Revision ID: 0092
Revises: 0091
"""

from __future__ import annotations

from alembic import op

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE protocol_t ADD VALUE IF NOT EXISTS 'provider'")


def downgrade() -> None:
    # Postgres has no DROP VALUE. A rollback that must remove the label
    # requires rebuilding the type, which is out of proportion to adding one
    # value nothing yet depends on - the same call 0043 and 0089 made.
    pass
