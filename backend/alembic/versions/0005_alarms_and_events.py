"""Alarms, alarm rules, events and alarm history.

Revision ID: 0005
Revises: 0004

The shape here is what turns a stream of threshold crossings into something an
operator can work:

* An alarm is a STATEFUL OBJECT keyed by (device, alarm_type, instance), not a
  row per occurrence. The unique partial index on that key is what makes
  raise/update/clear idempotent, which in turn is what makes at-least-once
  redelivery safe.
* Rules carry dwell and hysteresis. Without them a metric resting on its
  threshold raises and clears hundreds of times an hour.
* Symptoms point at their root cause rather than being deleted, so suppression
  stays a display decision and the evidence survives.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE alarm_state_t AS ENUM ('ACTIVE','ACKNOWLEDGED','CLEARED')")

    # ------------------------------------------------------------- rules
    op.create_table(
        "alarm_rule",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("alarm_type", sa.Text, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("device_types", pg.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("device_filter", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("metric_key", sa.Text),
        sa.Column("operator", sa.Text),
        sa.Column("threshold", sa.Numeric),
        # Clearing at the raise threshold makes a metric resting on the limit
        # flap forever; the deadband is the whole point.
        sa.Column("clear_threshold", sa.Numeric),
        sa.Column("dwell_samples", sa.Integer, nullable=False, server_default="3"),
        sa.Column("dwell_seconds", sa.Integer),
        sa.Column("clear_dwell_samples", sa.Integer, nullable=False, server_default="2"),
        sa.Column("severity", pg.ENUM(name="severity_t", create_type=False), nullable=False),
        sa.Column("stale_after_s", sa.Integer),
        sa.Column("message_tpl", sa.Text, nullable=False),
        sa.Column("attributes", pg.JSONB, nullable=False, server_default="{}"),
        sa.CheckConstraint(
            "operator IS NULL OR operator IN ('>','<','>=','<=','==','!=','absent')",
            name="ck_alarm_rule_operator"),
    )
    op.create_index("ix_alarm_rule_metric", "alarm_rule", ["metric_key"],
                    postgresql_where=sa.text("enabled AND metric_key IS NOT NULL"))

    # ------------------------------------------------------------ alarms
    op.create_table(
        "alarm",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("device_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        sa.Column("endpoint_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("device_endpoint.id", ondelete="SET NULL")),
        sa.Column("alarm_type", sa.Text, nullable=False),
        # ifIndex, sensor id, phase, BACnet object - empty for device-scoped.
        sa.Column("instance", sa.Text, nullable=False, server_default=""),
        sa.Column("rule_id", pg.UUID(as_uuid=True), sa.ForeignKey("alarm_rule.id")),
        sa.Column("severity", pg.ENUM(name="severity_t", create_type=False), nullable=False),
        sa.Column("prev_severity", pg.ENUM(name="severity_t", create_type=False)),
        sa.Column("state", pg.ENUM(name="alarm_state_t", create_type=False),
                  nullable=False, server_default="ACTIVE"),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("metric_key", sa.Text),
        sa.Column("trigger_value", sa.Numeric),
        sa.Column("threshold", sa.Numeric),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("occurrence_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_by", sa.Text),
        sa.Column("ack_note", sa.Text),
        sa.Column("cleared_at", sa.DateTime(timezone=True)),
        sa.Column("cleared_by", sa.Text),
        sa.Column("root_cause_alarm_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("alarm.id", ondelete="SET NULL")),
        sa.Column("is_symptom", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("attributes", pg.JSONB, nullable=False, server_default="{}"),
    )

    # THE constraint that makes raise/update/clear idempotent. Without it,
    # at-least-once redelivery produces duplicate alarms for one condition.
    op.execute("""
        CREATE UNIQUE INDEX alarm_active_key ON alarm (device_id, alarm_type, instance)
        WHERE state <> 'CLEARED'
    """)
    op.create_index("ix_alarm_state_severity", "alarm",
                    ["state", "severity", sa.text("last_seen DESC")])
    op.execute("CREATE INDEX ix_alarm_device_open ON alarm (device_id) "
               "WHERE state <> 'CLEARED'")
    op.execute("CREATE INDEX ix_alarm_root_cause ON alarm (root_cause_alarm_id) "
               "WHERE is_symptom")

    # ------------------------------------------------------------ events
    # Every trap is recorded even when its source cannot be resolved to a
    # device: silently dropping them is how an outage becomes "the DCIM never
    # saw it".
    op.execute("""
        CREATE TABLE event (
            id            bigserial,
            ts            timestamptz NOT NULL DEFAULT now(),
            device_id     uuid REFERENCES device(id) ON DELETE SET NULL,
            endpoint_id   uuid REFERENCES device_endpoint(id) ON DELETE SET NULL,
            source_ip     inet,
            event_type    text NOT NULL,
            source        text NOT NULL,
            severity      severity_t NOT NULL DEFAULT 'INFO',
            message       text NOT NULL,
            raw           jsonb NOT NULL DEFAULT '{}',
            alarm_id      uuid,
            dedup_key     text,
            PRIMARY KEY (ts, id)
        )
    """)
    op.execute("SELECT create_hypertable('event', 'ts', "
               "chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE)")
    op.execute("CREATE INDEX ix_event_device_ts ON event (device_id, ts DESC)")
    op.execute("CREATE INDEX ix_event_type_ts ON event (event_type, ts DESC)")
    op.execute("CREATE INDEX ix_event_unresolved ON event (ts DESC) "
               "WHERE device_id IS NULL")
    op.execute("SELECT add_retention_policy('event', INTERVAL '90 days')")

    # ----------------------------------------------------- alarm history
    op.execute("""
        CREATE TABLE alarm_history (
            ts        timestamptz NOT NULL DEFAULT now(),
            alarm_id  uuid NOT NULL,
            device_id uuid NOT NULL,
            action    text NOT NULL,
            severity  severity_t,
            actor     text,
            detail    jsonb NOT NULL DEFAULT '{}'
        )
    """)
    op.execute("SELECT create_hypertable('alarm_history', 'ts', "
               "chunk_time_interval => INTERVAL '30 days', if_not_exists => TRUE)")
    op.execute("CREATE INDEX ix_alarm_history_alarm ON alarm_history (alarm_id, ts DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS alarm_history")
    op.execute("DROP TABLE IF EXISTS event")
    op.drop_table("alarm")
    op.drop_table("alarm_rule")
    op.execute("DROP TYPE IF EXISTS alarm_state_t")
