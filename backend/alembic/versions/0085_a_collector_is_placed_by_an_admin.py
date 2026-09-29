"""A collector is placed by an admin, and can be drained, retired and revoked.

Three things a second collector needs that one never did.

**Where it is.** Site-aware sharding filtered on the sites a collector declared
about itself in its heartbeat - and no collector ever declared any, so every
collector was eligible for every site. With two datacenters, half of DC2's
endpoints hashed to DC1's collector, which cannot route to them. Placement is a
fact about the network the host sits on, known to the people who racked it,
so it is recorded here by an admin rather than volunteered by the process: a
typo in a heartbeat must not be able to re-shard the estate. NULL keeps the
single-collector meaning, "reaches every site".

**Whether it should have work.** `state` is the operator's intent, apart from
`status`, which is what the heartbeat says. `pending` is a collector that
announced itself but nobody has approved - it takes no work, so an unknown id
cannot claim half the fleet on its first fetch (PRTG's probe approval, for the
same reason). `draining` takes a collector out of the hash so its endpoints
move to the others in its site; `decommissioned` retires it and refuses its
token. Both replace the runbook's
`DELETE FROM collector_instance`, which deleted the history with the row.

**Whose token still works.** `token_generation` is folded into the derived
token, so one collector's token can be revoked by bumping its number instead
of rotating the master secret out from under the whole fleet. Zero is the
generation every token minted before this change already carries.

**Who ran a sweep.** `discovery_run.claimed_by` records the collector that took
a run, so its results can be refused from anyone else. A run assigned to nobody
could be reported on by any collector token at all, which let one collector
write findings - and raise missing-device alarms - into another's sweep.

Revision ID: 0085
Revises: 0084
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("collector_instance", sa.Column(
        "datacenter_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("datacenter.id", ondelete="SET NULL")))
    op.add_column("collector_instance", sa.Column(
        "state", sa.Text, nullable=False, server_default="active"))
    op.add_column("collector_instance", sa.Column(
        "state_changed_at", sa.DateTime(timezone=True)))
    op.add_column("collector_instance", sa.Column(
        "state_changed_by", sa.Text))
    op.add_column("collector_instance", sa.Column(
        "token_generation", sa.Integer, nullable=False, server_default="0"))
    op.create_check_constraint(
        "collector_instance_state", "collector_instance",
        "state IN ('pending', 'active', 'draining', 'decommissioned')")
    op.add_column("discovery_run", sa.Column("claimed_by", sa.Text))


def downgrade() -> None:
    op.drop_column("discovery_run", "claimed_by")
    op.drop_constraint("collector_instance_state", "collector_instance",
                       type_="check")
    for column in ("token_generation", "state_changed_by", "state_changed_at",
                   "state", "datacenter_id"):
        op.drop_column("collector_instance", column)
