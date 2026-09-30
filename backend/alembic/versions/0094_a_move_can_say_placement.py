"""A move can say "placement": its owner no longer serves where it lives.

Found bringing up the six-collector fleet: a discovery range moved 1426
endpoints into pools, the unplaced collector that held them could no longer
serve any of them, and the assigner wrote nothing - its mandatory-move test
asked only whether the owner was gone or draining, never whether it still
served the endpoint's pool or site. The serving path already fell back to
the plan (sharding.effective), so polling moved; the record and its history
never did. That move is now mandatory, and it gets its own reason: calling
it a "rebalance" would tell whoever reads the history that damping let an
imbalance through, when what happened is that an operator changed where the
endpoint or the collector lives.

Revision ID: 0094
Revises: 0093
"""

from __future__ import annotations

from alembic import op

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None

OLD_REASONS = ("initial", "rebalance", "pin", "pool_empty", "drain", "failover", "failback")
NEW_REASONS = (*OLD_REASONS, "placement")


def upgrade() -> None:
    op.drop_constraint("endpoint_assignment_reason", "endpoint_assignment", type_="check")
    op.create_check_constraint(
        "endpoint_assignment_reason", "endpoint_assignment",
        "reason IN (" + ", ".join(f"'{r}'" for r in NEW_REASONS) + ")")


def downgrade() -> None:
    op.execute("UPDATE endpoint_assignment SET reason = 'rebalance' WHERE reason = 'placement'")
    op.drop_constraint("endpoint_assignment_reason", "endpoint_assignment", type_="check")
    op.create_check_constraint(
        "endpoint_assignment_reason", "endpoint_assignment",
        "reason IN (" + ", ".join(f"'{r}'" for r in OLD_REASONS) + ")")
