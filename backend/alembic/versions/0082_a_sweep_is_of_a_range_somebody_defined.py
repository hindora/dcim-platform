"""A sweep is of a range somebody defined, swept by the collector that can reach it.

The sweep form listed the /24s of addresses already in inventory. On the
simulator that looked fine; on a real floor it fails four ways:

* A new site has nothing in inventory, so nothing is offered - and discovery is
  how inventory gets filled in the first place.
* A subnet nobody recorded never appears, which is exactly the case an audit is
  for.
* Everything is a /24. Real management networks are /22s for a hall of BMCs and
  /26s per row.
* Any collector claimed any run. With two sites, the collector in DC1 sweeping
  DC2's OOB network hears nothing, and the page reports a site's worth of
  devices missing.

So ranges are records: a CIDR of any prefix down to /20 (the 4094-host ceiling
the collector refuses past), a name, the site and plane it belongs to, the
collector that can reach it (NULL = any), and addresses to leave alone -
gateways, and old controllers that misbehave when scanned. SolarWinds, LibreNMS,
Device42 and dcTrack all model discovery the same way.

Schedules now point at ranges instead of carrying CIDR text, so editing a range
changes what its schedules sweep. Existing schedules are converted: each of
their subnets becomes a range (or joins an identical one).

A run records its target collector and the ranges it swept. `scope` gains
`exclude`, which the collector must honour - and the API hands a run with
exclusions only to a collector that says it can.

Revision ID: 0082
Revises: 0081
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None

PURPOSES = ("it_oob", "bms", "production", "other")


def upgrade() -> None:
    op.create_table(
        "discovery_range",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("cidr", postgresql.CIDR, nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("datacenter_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("datacenter.id", ondelete="SET NULL")),
        sa.Column("purpose", sa.Text),
        # No FK: collectors register themselves by heartbeat, and a range may
        # be assigned to one that has not checked in yet.
        sa.Column("collector_id", sa.Text),
        sa.Column("exclusions", postgresql.ARRAY(postgresql.CIDR), nullable=False,
                  server_default=sa.text("'{}'::cidr[]")),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.Text),
        sa.Column("created_by", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("family(cidr) = 4", name="ipv4"),
        # The collector refuses a sweep past 4096 addresses rather than
        # truncating it. A /20 is 4094 hosts; anything wider could never run.
        sa.CheckConstraint("masklen(cidr) >= 20", name="sweepable"),
        sa.CheckConstraint(
            "purpose IS NULL OR purpose IN ("
            + ", ".join(f"'{p}'" for p in PURPOSES) + ")", name="purpose"),
    )

    op.add_column("discovery_run", sa.Column("collector_id", sa.Text))
    op.add_column("discovery_run", sa.Column(
        "range_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
        nullable=False, server_default=sa.text("'{}'::uuid[]")))
    op.create_index("ix_discovery_run_pending_collector", "discovery_run",
                    ["collector_id"], postgresql_where=sa.text("status = 'pending'"))

    # ------------------------------------------------ schedules -> ranges
    op.add_column("discovery_schedule", sa.Column(
        "range_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
        nullable=False, server_default=sa.text("'{}'::uuid[]")))
    # network(): a subnet typed with host bits set ("10.51.11.5/24") is the /24
    # it names, and CAST to cidr refuses host bits.
    op.execute("""
        INSERT INTO discovery_range (cidr, name, created_by)
        SELECT DISTINCT network(CAST(s AS inet))::cidr,
                        network(CAST(s AS inet))::text, 'migration 0082'
          FROM discovery_schedule, unnest(subnets) AS s
         WHERE masklen(CAST(s AS inet)) >= 20
        ON CONFLICT (cidr) DO NOTHING
    """)
    op.execute("""
        UPDATE discovery_schedule sch
           SET range_ids = sub.ids
          FROM (SELECT sch2.id,
                       array_agg(DISTINCT r.id) AS ids
                  FROM discovery_schedule sch2, unnest(sch2.subnets) AS s
                  JOIN discovery_range r
                    ON r.cidr = network(CAST(s AS inet))::cidr
                 GROUP BY sch2.id) sub
         WHERE sub.id = sch.id
    """)
    # A schedule whose subnets were all too wide to be a range cannot run any
    # more. Paused rather than deleted: somebody made it, and should see it.
    op.execute("""
        UPDATE discovery_schedule SET enabled = false
         WHERE cardinality(range_ids) = 0
    """)
    op.drop_constraint("ck_discovery_schedule_subnets", "discovery_schedule",
                       type_="check")
    op.drop_column("discovery_schedule", "subnets")


def downgrade() -> None:
    op.add_column("discovery_schedule", sa.Column(
        "subnets", postgresql.ARRAY(sa.Text), nullable=False,
        server_default=sa.text("'{}'::text[]")))
    op.execute("""
        UPDATE discovery_schedule sch
           SET subnets = COALESCE((SELECT array_agg(r.cidr::text)
                                     FROM discovery_range r
                                    WHERE r.id = ANY(sch.range_ids)), '{}')
    """)
    op.execute("DELETE FROM discovery_schedule WHERE cardinality(subnets) = 0")
    op.create_check_constraint("ck_discovery_schedule_subnets", "discovery_schedule",
                               "cardinality(subnets) >= 1")
    op.drop_column("discovery_schedule", "range_ids")
    op.drop_index("ix_discovery_run_pending_collector", table_name="discovery_run")
    op.drop_column("discovery_run", "range_ids")
    op.drop_column("discovery_run", "collector_id")
    op.drop_table("discovery_range")
