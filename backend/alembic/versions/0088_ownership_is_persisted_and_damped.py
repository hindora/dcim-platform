"""Ownership is persisted, not recomputed on every request, and damped.

docs/26 Phase 5. Two independent problems with the live-recompute design
from migration 0085/services/sharding.py:

1. **No stable record of who owns what.** `build_assignment` and the
   ingest worker's `OwnershipGuard` each independently call
   `sharding.plan()` on their own schedule. With more than one API or
   ingest-worker process, each holds its own in-memory copy, computed at a
   slightly different moment - harmless while the fleet is stable, a real
   source of disagreement during the exact window (a collector joining,
   leaving, draining) where correctness matters most. Nothing anywhere
   records "who polled this UPS at 03:12" past the next recompute.

2. **No damping.** Rendezvous hashing already limits a membership change to
   moving only the share that has to move - about 1/N - but nothing stops
   *every* recompute from re-deriving the same 1/N move fresh, and nothing
   distinguishes a real capacity change from routine heartbeat jitter
   (`healthy` flipping as a heartbeat lands a few seconds late). Persisting
   the plan is what makes damping possible at all: there has to be a
   "current" to compare a proposed "target" against.

**`collector_pool`** is the new placement granularity: site × plane
(`it_oob`/`bms`/`production`/`other` - matching `discovery_range.purpose`
from migration 0082 exactly, not the plan doc's illustrative "provider",
so a pool's plane always agrees with the ranges endpoints are actually
discovered on). `device_endpoint.pool_id` is an explicit override; NULL
means "inherit" - resolved at plan time from the endpoint's own address
falling inside a `discovery_range`'s CIDR, joined with its device's
datacenter, exactly as the plan describes. `collector_instance.pool_id` is
the new, finer-grained alternative to `datacenter_id` for where an admin
places a collector; NULL keeps today's site-only (or site-less) placement
working completely unchanged - no flag day, the same choice every
migration since 0085 has made.

**`endpoint_assignment`** is the persisted plan itself: one row per
endpoint that has ever been assigned, the collector, an epoch that bumps on
every write (cheap staleness/version check for a reader), when, and why
(`initial`/`rebalance`/`pin`/`pool_empty`/`drain`). `collector_id IS NULL`
with `reason = 'pool_empty'` is a real, persisted state - an endpoint whose
pool currently has no healthy accepting member - not the absence of a row.

This migration adds the tables and columns only. The assigner that writes
`endpoint_assignment` (`app/services/assigner.py`) and the read-side
changes to `build_assignment`/`OwnershipGuard` are separate, and endpoints
with no pool at all - every endpoint in a deployment that has not created
any `collector_pool` rows yet, which after this migration is every existing
deployment - keep going through the pre-Phase-5 live-recompute path
unchanged until an admin actually creates a pool.

Revision ID: 0088
Revises: 0087
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None

PLANES = ("it_oob", "bms", "production", "other")
ASSIGNMENT_REASONS = ("initial", "rebalance", "pin", "pool_empty", "drain")


def upgrade() -> None:
    op.create_table(
        "collector_pool",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("datacenter_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("datacenter.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plane", sa.Text, nullable=False),
        # Additional CIDRs this pool's members must be able to reach, beyond
        # whatever discovery_range already implies - a trap VIP or BBMD
        # network a pool's collectors share that is not itself a sweep
        # target.
        sa.Column("cidrs", postgresql.ARRAY(postgresql.CIDR), nullable=False,
                  server_default=sa.text("'{}'::cidr[]")),
        sa.Column("trap_vip", postgresql.INET),
        sa.Column("bbmd_settings", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        # Weighted points/s this pool's capacity model treats as its budget;
        # NULL means unset - no utilisation alarm until an operator sizes it.
        sa.Column("rate_budget_points_per_s", sa.Integer),
        sa.Column("min_members", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "plane IN (" + ", ".join(f"'{p}'" for p in PLANES) + ")",
            name="collector_pool_plane"),
        sa.UniqueConstraint("datacenter_id", "plane", name="collector_pool_site_plane"),
    )

    op.add_column("device_endpoint", sa.Column(
        "pool_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("collector_pool.id", ondelete="SET NULL")))
    op.add_column("collector_instance", sa.Column(
        "pool_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("collector_pool.id", ondelete="SET NULL")))

    op.create_table(
        "endpoint_assignment",
        # No CASCADE-through-truncate surprise: an endpoint row disappearing
        # removes its assignment with it, the normal lifecycle when a
        # device is deleted - see the foreign key added below.
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("collector_id", sa.Text),
        sa.Column("epoch", sa.BigInteger, nullable=False, server_default="1"),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("reason", sa.Text, nullable=False),
        sa.CheckConstraint(
            "reason IN (" + ", ".join(f"'{r}'" for r in ASSIGNMENT_REASONS) + ")",
            name="endpoint_assignment_reason"),
    )
    op.create_foreign_key(
        "endpoint_assignment_endpoint_id_fkey", "endpoint_assignment",
        "device_endpoint", ["endpoint_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_endpoint_assignment_collector", "endpoint_assignment",
                    ["collector_id"])


def downgrade() -> None:
    op.drop_table("endpoint_assignment")
    op.drop_column("collector_instance", "pool_id")
    op.drop_column("device_endpoint", "pool_id")
    op.drop_table("collector_pool")
