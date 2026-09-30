"""A collector says how long it has been continuously healthy.

docs/26 Phase 6. `last_heartbeat` alone answers "is it healthy right now" -
what `live_collectors`'s existing `stale_after_s` cutoff already checks -
but failback damping ("restarting the primary does not move anything back
for 10 minutes") needs a different question: how long has it been healthy
WITHOUT a gap, not just at this instant. A collector that flapped nine
times in the last five minutes and a collector that has been rock solid for
a week can both show "healthy: true, age: 3s" on the same heartbeat; only
`healthy_since` tells them apart, and telling them apart is the entire
point of failback damping - reclaiming endpoints from a standby the moment
a flapping primary's heartbeat happens to land is the exact "heroics" this
phase's own title warns against.

`healthy_since` is maintained at heartbeat-write time, in
`_handle_heartbeat`: if the gap since the PREVIOUS heartbeat exceeded the
stale threshold (60s, matching `live_collectors`'s own cutoff), or there is
no previous heartbeat at all, this heartbeat resets `healthy_since` to now -
it just became healthy again (or healthy for the first time). Otherwise
`healthy_since` is left exactly as it was: still the same unbroken streak.

Revision ID: 0089
Revises: 0088
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("collector_instance",
                  sa.Column("healthy_since", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("collector_instance", "healthy_since")
