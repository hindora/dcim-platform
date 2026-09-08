"""Compress the raw tables that were not, sooner, and bound the alarm log.

Three loose ends from the same disk audit as 0051 and 0052.

**Raw samples compress after one day, not seven.** The 7-day lag dates from
when compression was new and a chunk might still receive late rows. Timescale
has taken inserts into compressed chunks for years, the collector writes within
seconds of observing, and the 1m refresh reads only three hours back. The lag
bought nothing and cost 16 GB uncompressed at any moment - six days of chunks
that compress 25:1 the day they age out.

**Booleans and text compress at all.** They had the same segmentby shape as the
numeric table and never got the policy; at 1,386 endpoints the boolean table
reached 7.4 GB with its indexes larger than its heap. A boolean stream is
nearly constant, so it compresses better than the numeric one. Their chunks
also go from 7 days to 1 (new chunks only; the existing ones keep their
width): a compression policy acts on a chunk only once its whole span is older
than the lag, and a 7-day chunk with a 1-day lag left up to two weeks of rows
uncompressed. Matching the numeric table's 1-day chunks makes the lag mean what
it says.

**Alarm history keeps two years.** It is an audit trail of every raise, ack
and clear, small today, and it was the only hypertable with no retention at
all. Two years is the usual retention for operational audit records and
comfortably outlives any alarm-rate trend anyone will draw.

Revision ID: 0053
Revises: 0052
"""

from __future__ import annotations

from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None

#: (table, chunk interval after, chunk interval before)
RECHUNK = (
    ("telemetry_bool", "1 day", "7 days"),
    ("telemetry_text", "1 day", "7 days"),
)

ALARM_RETENTION = "2 years"


def _compression_lag(table: str, lag: str) -> None:
    op.execute(f"SELECT remove_compression_policy('{table}', if_exists => TRUE)")
    op.execute(f"SELECT add_compression_policy('{table}', INTERVAL '{lag}', "
               f"if_not_exists => TRUE)")


def upgrade() -> None:
    _compression_lag("telemetry_sample", "1 day")

    for table, width, _ in RECHUNK:
        op.execute(f"""
            ALTER TABLE {table} SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'device_id, metric_id, instance',
                timescaledb.compress_orderby   = 'ts DESC'
            )
        """)
        _compression_lag(table, "1 day")
        op.execute(f"SELECT set_chunk_time_interval('{table}', INTERVAL '{width}')")

    op.execute(f"SELECT add_retention_policy('alarm_history', "
               f"INTERVAL '{ALARM_RETENTION}', if_not_exists => TRUE)")


def downgrade() -> None:
    op.execute("SELECT remove_retention_policy('alarm_history', if_exists => TRUE)")

    for table, _, width in RECHUNK:
        op.execute(f"SELECT set_chunk_time_interval('{table}', INTERVAL '{width}')")
        op.execute(f"SELECT remove_compression_policy('{table}', if_exists => TRUE)")
        op.execute(f"""
            SELECT decompress_chunk(c, if_compressed => TRUE)
              FROM show_chunks('{table}') c
        """)
        op.execute(f"ALTER TABLE {table} SET (timescaledb.compress = false)")

    _compression_lag("telemetry_sample", "7 days")
