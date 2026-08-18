"""TimescaleDB hypertables, compression, retention and continuous aggregates.

Revision ID: 0002
Revises: 0001

Notes
-----
Continuous aggregates cannot be created inside a transaction block, so this
migration commits the surrounding transaction first and then issues DDL in
autocommit. That is why the statements below are deliberately idempotent
(IF NOT EXISTS / catalog guards): a failure part-way cannot be rolled back.

Hierarchical continuous aggregates (5m built from 1m) need TimescaleDB 2.9+.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


TELEMETRY_TABLES = (
    # (table, value column type, chunk interval)
    ("telemetry_sample", "double precision", "1 day"),
    ("telemetry_bool", "boolean", "7 days"),
    ("telemetry_text", "text", "7 days"),
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    for table, coltype, chunk in TELEMETRY_TABLES:
        op.execute(f"""
            CREATE TABLE {table} (
                ts        timestamptz NOT NULL,
                device_id uuid        NOT NULL,
                metric_id smallint    NOT NULL REFERENCES metric(id),
                instance  text        NOT NULL DEFAULT '',
                value     {coltype}   NOT NULL,
                quality   text        NOT NULL DEFAULT 'good',
                PRIMARY KEY (device_id, metric_id, instance, ts)
            )
        """)
        # The primary key doubles as the idempotency guarantee for at-least-once
        # delivery: a redelivered batch conflicts and is discarded.
        op.execute(
            f"SELECT create_hypertable('{table}', 'ts', "
            f"chunk_time_interval => INTERVAL '{chunk}', if_not_exists => TRUE)")
        op.execute(f"CREATE INDEX ix_{table}_metric_ts ON {table} (metric_id, ts DESC)")

    op.execute("""
        ALTER TABLE telemetry_sample SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'device_id, metric_id, instance',
            timescaledb.compress_orderby   = 'ts DESC'
        )
    """)
    op.execute("SELECT add_compression_policy('telemetry_sample', INTERVAL '7 days')")
    op.execute("SELECT add_retention_policy('telemetry_sample', INTERVAL '90 days')")
    op.execute("SELECT add_retention_policy('telemetry_bool', INTERVAL '180 days')")
    op.execute("SELECT add_retention_policy('telemetry_text', INTERVAL '180 days')")

    # Per-poll outcomes. This is what makes "why is this endpoint flapping"
    # answerable, and it is the input to the collector-health view.
    op.execute("""
        CREATE TABLE poll_result (
            ts               timestamptz NOT NULL,
            endpoint_id      uuid        NOT NULL,
            collector_id     text        NOT NULL,
            success          boolean     NOT NULL,
            latency_ms       integer,
            error_class      text,
            metrics_returned integer
        )
    """)
    op.execute("SELECT create_hypertable('poll_result', 'ts', "
               "chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE)")
    op.execute("CREATE INDEX ix_poll_result_endpoint_ts ON poll_result (endpoint_id, ts DESC)")
    op.execute("SELECT add_retention_policy('poll_result', INTERVAL '14 days')")

    # ---------------------------------------------------------------------
    # Continuous aggregates. Without these a 30-day chart reads raw samples.
    # ---------------------------------------------------------------------
    conn = op.get_bind()
    conn.execute(sa.text("COMMIT"))

    conn.execute(sa.text("""
        CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_1m
        WITH (timescaledb.continuous) AS
        SELECT time_bucket('1 minute', ts) AS bucket,
               device_id, metric_id, instance,
               avg(value)      AS avg_value,
               min(value)      AS min_value,
               max(value)      AS max_value,
               last(value, ts) AS last_value,
               count(*)        AS sample_count
        FROM telemetry_sample
        GROUP BY bucket, device_id, metric_id, instance
        WITH NO DATA
    """))

    conn.execute(sa.text("""
        CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_5m
        WITH (timescaledb.continuous) AS
        SELECT time_bucket('5 minutes', bucket) AS bucket,
               device_id, metric_id, instance,
               avg(avg_value)       AS avg_value,
               min(min_value)       AS min_value,
               max(max_value)       AS max_value,
               last(last_value, bucket) AS last_value,
               sum(sample_count)    AS sample_count
        FROM telemetry_1m
        GROUP BY 1, device_id, metric_id, instance
        WITH NO DATA
    """))

    conn.execute(sa.text("""
        CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_1h
        WITH (timescaledb.continuous) AS
        SELECT time_bucket('1 hour', bucket) AS bucket,
               device_id, metric_id, instance,
               avg(avg_value)       AS avg_value,
               min(min_value)       AS min_value,
               max(max_value)       AS max_value,
               last(last_value, bucket) AS last_value,
               sum(sample_count)    AS sample_count
        FROM telemetry_5m
        GROUP BY 1, device_id, metric_id, instance
        WITH NO DATA
    """))

    for view, start, end, sched in (
        ("telemetry_1m", "3 hours", "1 minute", "1 minute"),
        ("telemetry_5m", "1 day", "5 minutes", "5 minutes"),
        ("telemetry_1h", "7 days", "1 hour", "30 minutes"),
    ):
        conn.execute(sa.text(f"""
            SELECT add_continuous_aggregate_policy('{view}',
                start_offset      => INTERVAL '{start}',
                end_offset        => INTERVAL '{end}',
                schedule_interval => INTERVAL '{sched}',
                if_not_exists     => TRUE)
        """))

    # 1h is deliberately kept forever: it is what capacity trending needs and
    # it is tiny.
    conn.execute(sa.text("SELECT add_retention_policy('telemetry_1m', INTERVAL '1 year')"))
    conn.execute(sa.text("SELECT add_retention_policy('telemetry_5m', INTERVAL '2 years')"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("COMMIT"))
    for view in ("telemetry_1h", "telemetry_5m", "telemetry_1m"):
        conn.execute(sa.text(f"DROP MATERIALIZED VIEW IF EXISTS {view}"))
    for table in ("poll_result", "telemetry_text", "telemetry_bool", "telemetry_sample"):
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))
