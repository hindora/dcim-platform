"""Discovery staging: runs, candidates, promotion.

Revision ID: 0012
Revises: 0011

A device that answers on the management network but appears nowhere in
inventory is the thing an audit exists to find: something was installed and
never recorded, or recorded and then renumbered, or is not supposed to be there
at all. Until it is in inventory nothing else in this platform can see it - it
has no endpoints, no telemetry, no alarms.

Staged rather than auto-created. Discovery guesses; inventory is a record of
fact, and a sweep that silently invented devices would make the record worse
rather than better. A candidate waits for someone to promote or ignore it.

Note what is deliberately NOT here: an alarm row. alarm.device_id is NOT NULL,
so a finding about a device that does not exist in inventory cannot be
expressed as an alarm at all. The candidate row IS the flag, and the API
surfaces it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discovery_run",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        # snmp_sweep today; bacnet_whois and redfish_probe are the same shape.
        sa.Column("method", sa.Text, nullable=False),
        sa.Column("scope", sa.dialects.postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("found", sa.Integer, nullable=False, server_default="0"),
        sa.Column("promoted", sa.Integer, nullable=False, server_default="0"),
        # pending -> running -> done | failed. The collector claims pending runs.
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("error", sa.Text),
    )
    op.create_index("ix_discovery_run_status", "discovery_run", ["status"],
                    postgresql_where=sa.text("status = 'pending'"))

    op.create_table(
        "discovery_candidate",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("discovery_run.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("address", sa.dialects.postgresql.INET),
        # postgresql.ENUM, not sa.Enum: the generic one emits CREATE TYPE even
        # with create_type=False, and protocol_t already exists.
        sa.Column("protocol",
                  sa.dialects.postgresql.ENUM(name="protocol_t",
                                              create_type=False),
                  nullable=False),
        # Whatever the probe could read: sysDescr, sysObjectID, sysName.
        sa.Column("identity", sa.dialects.postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("suggested_device_type", sa.Text),
        sa.Column("suggested_vendor", sa.Text),
        sa.Column("suggested_model", sa.Text),
        # Non-NULL means this responder is already known. Recorded rather than
        # dropped: "the sweep saw 900 devices and 894 were expected" is a more
        # useful answer than a list of six surprises with no denominator.
        sa.Column("matched_device_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("device.id", ondelete="SET NULL")),
        sa.Column("status", sa.Text, nullable=False, server_default="new"),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_discovery_candidate_run", "discovery_candidate", ["run_id"])
    # One open candidate per address and protocol, however many times it is
    # rediscovered - otherwise a nightly sweep grows a new row for the same
    # unmanaged device every night.
    op.create_index("uq_discovery_candidate_open", "discovery_candidate",
                    ["address", "protocol"], unique=True,
                    postgresql_where=sa.text("status = 'new'"))


def downgrade() -> None:
    op.drop_table("discovery_candidate")
    op.drop_table("discovery_run")
