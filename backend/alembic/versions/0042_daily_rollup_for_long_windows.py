"""A month of chart had no bucket coarse enough to draw it.

The aggregate ladder stopped at 1 hour, so every window longer than about eight
days routed to hourly and overshot the point budget the router exists to hold:
30 days is 720 points per series and 90 days is 2,160, drawn into a plot roughly
650 units wide. At that density a noisy signal overprints into a band and the
trajectory - the only thing a month view is for - disappears inside it.

Daily buckets put 30 days at 30 points and 90 at 90, which is a shape a person
can read.

Built on telemetry_1h rather than on the raw table, matching the 1m -> 5m -> 1h
chain already here. Each level aggregates the one below, so materialising a day
reads 24 rows instead of rescanning a 14 GB hypertable, and the same
avg/min/max/last/count columns carry through unchanged.

Kept forever, with no retention policy, for the same reason 1h has none: daily
buckets are what capacity trending reads and the whole view is a rounding error
next to the raw table.
"""

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

VIEW = "telemetry_1d"
SOURCE = "telemetry_1h"


def upgrade() -> None:
    # autocommit: CREATE MATERIALIZED VIEW ... WITH (timescaledb.continuous)
    # and the policy calls cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        op.execute(f"""
            CREATE MATERIALIZED VIEW IF NOT EXISTS {VIEW}
            WITH (timescaledb.continuous) AS
            SELECT time_bucket('1 day', bucket) AS bucket,
                   device_id, metric_id, instance,
                   avg(avg_value)           AS avg_value,
                   min(min_value)           AS min_value,
                   max(max_value)           AS max_value,
                   last(last_value, bucket) AS last_value,
                   sum(sample_count)        AS sample_count
            FROM {SOURCE}
            GROUP BY 1, device_id, metric_id, instance
            WITH NO DATA
        """)

        # end_offset of a full day: a daily bucket is only correct once its day
        # has closed, and a hierarchical aggregate must also stay behind the
        # parent it reads. start_offset spans the longest window the charts
        # offer, so the policy maintains everything a reader can ask for.
        op.execute(f"""
            SELECT add_continuous_aggregate_policy('{VIEW}',
                start_offset      => INTERVAL '90 days',
                end_offset        => INTERVAL '1 day',
                schedule_interval => INTERVAL '1 hour',
                if_not_exists     => TRUE)
        """)

        # Backfill now rather than waiting for the first scheduled run, so a
        # 30-day chart works immediately after deploy instead of an hour later.
        # Bounded by the source: telemetry_1h holds hourly rows, so this reads
        # thousands, not millions.
        op.execute(f"CALL refresh_continuous_aggregate('{VIEW}', NULL, NULL)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        # The policy job goes first. It is run by TimescaleDB's scheduler, which
        # knows nothing about a migration in progress - a refresh firing against
        # the view while it is being dropped touches the same catalog tuples and
        # aborts with "tuple concurrently deleted". That fails intermittently,
        # which is worse than failing always because it reads as a flake.
        op.execute(f"""
            DO $$
            DECLARE job_id integer;
            BEGIN
                FOR job_id IN
                    SELECT j.job_id FROM timescaledb_information.jobs j
                     WHERE j.hypertable_name IN (
                               SELECT materialization_hypertable_name
                                 FROM timescaledb_information.continuous_aggregates
                                WHERE view_name = '{VIEW}')
                LOOP
                    PERFORM delete_job(job_id);
                END LOOP;
            END $$
        """)
        op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {VIEW}")
