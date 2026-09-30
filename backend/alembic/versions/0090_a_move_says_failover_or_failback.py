"""A move says failover or failback, not just "drain".

docs/26 Phase 6. Migration 0088's endpoint_assignment.reason distinguished
an operator's own drain from everything else, but not from what Phase 6
adds: an automatic move because a pool's member went silent past
FAILOVER_AFTER_S (`apply_ha_policy` in services/sharding.py), and the
matching move back once it has been healthy long enough
(FAILBACK_AFTER_S). A drain is a decision; a failover is the platform
acting on a fact - the audit trail should say which happened, not file
both under the word for the one an operator actually chose.

Revision ID: 0090
Revises: 0089
"""

from __future__ import annotations

from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None

OLD_REASONS = ("initial", "rebalance", "pin", "pool_empty", "drain")
NEW_REASONS = (*OLD_REASONS, "failover", "failback")


def upgrade() -> None:
    op.drop_constraint("endpoint_assignment_reason", "endpoint_assignment", type_="check")
    op.create_check_constraint(
        "endpoint_assignment_reason", "endpoint_assignment",
        "reason IN (" + ", ".join(f"'{r}'" for r in NEW_REASONS) + ")")


def downgrade() -> None:
    op.drop_constraint("endpoint_assignment_reason", "endpoint_assignment", type_="check")
    op.create_check_constraint(
        "endpoint_assignment_reason", "endpoint_assignment",
        "reason IN (" + ", ".join(f"'{r}'" for r in OLD_REASONS) + ")")
