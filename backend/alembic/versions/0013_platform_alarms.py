"""Platform alarms: an alarm that is about the platform, not about a device.

The DCIM has to monitor itself, and those findings have to appear in the same
alarm list as device faults - an operator who cannot tell "the datacenter is
quiet" from "the collector died" has no monitoring at all. That means an alarm
must be able to exist without a device, which the table did not allow.

Two details make the difference between this working and looking like it works.

**The unique index.** ``alarm_active_key`` is what makes raising an alarm
idempotent: the upsert collides on it and bumps the existing row instead of
inserting a second one. Postgres treats NULLs as distinct in a unique index by
default, so with a null device_id every evaluation cycle would have inserted a
NEW alarm - one ingest_lag_high per evaluation, forever. NULLS NOT DISTINCT
(Postgres 15+, and this runs 16) makes the null device_id collide like any other
value.

**The reader.** The alarm list joined device with an INNER join, which would
have silently dropped every platform alarm - the alarms that say the monitoring
itself is broken would be the ones the alarm list hides. That is fixed in the
repository, but it is the same change: it only became possible to get wrong
once device_id could be null.
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE alarm ALTER COLUMN device_id DROP NOT NULL")
    # History follows the alarm. Without this the raise succeeds and the audit
    # write fails, which is the worst of the three possible outcomes.
    op.execute("ALTER TABLE alarm_history ALTER COLUMN device_id DROP NOT NULL")

    # Recreated rather than altered: NULLS NOT DISTINCT is a property of the
    # index, so the old one has to go.
    op.execute("DROP INDEX IF EXISTS alarm_active_key")
    op.execute("""
        CREATE UNIQUE INDEX alarm_active_key
            ON alarm (device_id, alarm_type, instance) NULLS NOT DISTINCT
         WHERE state <> 'CLEARED'
    """)

    # Platform alarms are queried as a group ("is the platform healthy?") far
    # more often than they are queried by type, and there are few of them.
    op.execute("""
        CREATE INDEX ix_alarm_platform
            ON alarm (last_seen DESC)
         WHERE device_id IS NULL AND state <> 'CLEARED'
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_alarm_platform")
    # A platform alarm cannot survive the column becoming NOT NULL again, and
    # leaving it to fail on a stray row would make the downgrade unrunnable.
    op.execute("DELETE FROM alarm WHERE device_id IS NULL")
    op.execute("DROP INDEX IF EXISTS alarm_active_key")
    op.execute("""
        CREATE UNIQUE INDEX alarm_active_key
            ON alarm (device_id, alarm_type, instance)
         WHERE state <> 'CLEARED'
    """)
    op.execute("DELETE FROM alarm_history WHERE device_id IS NULL")
    op.execute("ALTER TABLE alarm_history ALTER COLUMN device_id SET NOT NULL")
    op.execute("ALTER TABLE alarm ALTER COLUMN device_id SET NOT NULL")
