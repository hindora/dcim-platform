"""The one-minute aggregate was a wider copy of the raw table, kept for a year.

Power and most other metrics are polled every 60 to 120 s. A one-minute bucket
over that cadence holds 1.14 samples on average, so `telemetry_1m` did not
reduce anything: it carried 88% of the raw row count with five value columns
where raw has one, plus four indexes, and it was never compressed. At 1,386
endpoints it grew at about 0.9 GB per day of collection and had reached 21 GB
against 17 GB of raw. Its one-year window projected to 300 GB.

It is not dropped, because `telemetry_5m`, `telemetry_1h` and `telemetry_1d`
are stacked on it. Recreating the chain against the raw table would backfill
only what raw still holds, and the hourly and daily tiers are kept forever
precisely so that capacity trending outlives the raw window.

So it keeps the job it is actually good at and nothing more. The chart router
sends a window to 1m only when it produces at most 200 points per series,
which is about three hours, and the 5m refresh reads 1m one day back. Seven
days covers both with room to spare. The two readers that pulled 1m over 30
to 90 days - the capacity report and the forecast's daily series - now read
5m (that change is in the repositories, not here); both sum one value per
device per bucket and take a percentile, which is width-agnostic, and a
five-minute mean is already coarser than the 15-minute demand interval a
utility bills on.

Compression is switched on for 1m and 5m while at it. Every aggregate in the
ladder was uncompressed; the raw table compresses 25:1 with the same
segmentby, and the aggregates are the same shape.

1m chunks are ten days wide, so the retention job drops one only once its
whole span is older than seven days. `drop_chunks` after the upgrade if the
space is needed today.

Revision ID: 0052
Revises: 0051
"""

from __future__ import annotations

from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None

#: (view, new retention, old retention, compress_after). The old value is what
#: the downgrade restores. telemetry_5m keeps its two years; it only gains
#: compression.
AGGREGATES = (
    ("telemetry_1m", "7 days", "1 year", "1 day"),
    ("telemetry_5m", "2 years", "2 years", "2 days"),
)


def _retention(view: str, window: str) -> None:
    # Timescale has no ALTER for a policy: drop it and add it back.
    op.execute(f"SELECT remove_retention_policy('{view}', if_exists => TRUE)")
    op.execute(f"SELECT add_retention_policy('{view}', INTERVAL '{window}')")


def upgrade() -> None:
    for view, window, _, compress_after in AGGREGATES:
        _retention(view, window)
        # Same segmentby as the raw table (0002): one compressed batch per
        # series, ordered by time within it. compress_after must exceed the
        # aggregate's own refresh start_offset (3 h for 1m, 1 day for 5m) or
        # the refresh would be writing into a compressed chunk.
        op.execute(f"""
            ALTER MATERIALIZED VIEW {view} SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'device_id, metric_id, instance',
                timescaledb.compress_orderby   = 'bucket DESC'
            )
        """)
        op.execute(f"SELECT add_compression_policy('{view}', "
                   f"INTERVAL '{compress_after}', if_not_exists => TRUE)")


def downgrade() -> None:
    for view, _, window, _ in AGGREGATES:
        op.execute(f"SELECT remove_compression_policy('{view}', if_exists => TRUE)")
        # Chunks compressed meanwhile must be decompressed before compression
        # can be switched off on the view.
        op.execute(f"""
            SELECT decompress_chunk(c, if_compressed => TRUE)
              FROM show_chunks('{view}') c
        """)
        op.execute(f"ALTER MATERIALIZED VIEW {view} SET (timescaledb.compress = false)")
        _retention(view, window)
