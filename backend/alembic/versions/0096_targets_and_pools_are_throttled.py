"""Targets and pools are throttled, not only measured: per-address limits.

docs/26 Phase 9 asked for per-target rate budgets on BMS supervisors and
serial gateways, and Phase 5's pool budget was measured and alarmed while
"nothing throttles to the budget yet". The collector now enforces both
(collector/internal/throttle); this is where the per-target half is set.

- collector_pool.target_limits - per-protocol defaults for the pool's
  network, e.g. {"modbus": {"max_concurrent": 1, "min_interval_ms": 100}}:
  how many polls may be in flight at one address and the minimum gap
  between their starts (Kepware's inter-request delay). A pool is a
  management network, and the gateways on one network are usually one
  model, so the pool is where an operator sets it once.
- device_endpoint.target_limit - one endpoint's override, for the odd
  device on that network: a legacy NMC card that wants one request at a
  time, a gateway with a slower serial line. NULL: the pool default.

No CHECK on the inner shape: the API validates it, and an unknown key is
ignored by the collector rather than refused, so a newer platform's field
never breaks an older collector.

Revision ID: 0096
Revises: 0095
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("collector_pool", sa.Column(
        "target_limits", postgresql.JSONB, nullable=False,
        server_default=sa.text("'{}'::jsonb")))
    op.create_check_constraint("collector_pool_target_limits_object", "collector_pool",
                               "jsonb_typeof(target_limits) = 'object'")
    op.add_column("device_endpoint", sa.Column("target_limit", postgresql.JSONB))
    op.create_check_constraint("device_endpoint_target_limit_object", "device_endpoint",
                               "target_limit IS NULL OR jsonb_typeof(target_limit) = 'object'")


def downgrade() -> None:
    # Each CHECK goes with its column. Dropping it by name first failed: the
    # metadata naming convention stores it as ck_<table>_<name>.
    op.drop_column("device_endpoint", "target_limit")
    op.drop_column("collector_pool", "target_limits")
