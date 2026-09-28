"""A schedule can run at a time of day, in the site's timezone.

An interval schedule fires 24 hours after it last did, so its time of day drifts
with whenever somebody created it or pressed Run now. Real sites sweep their OOB
and BMS networks in a quiet window - "nightly at 02:00", "weekdays at 02:00" -
because scanning during business hours is what change boards object to. That is
how SolarWinds, ServiceNow Discovery and Device42 schedule it.

`run_at` + `days` (ISO weekday numbers, 1 = Monday) + `timezone` (IANA). NULL
`run_at` keeps the interval behaviour. For a timed schedule `interval_hours` is
the longest gap between two of its runs (24 daily, 72 for weekdays over a
weekend, 168 weekly), which is what says when an audit has gone stale.

Revision ID: 0083
Revises: 0082
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("discovery_schedule", sa.Column("run_at", sa.Time))
    op.add_column("discovery_schedule", sa.Column(
        "days", postgresql.ARRAY(sa.SmallInteger)))
    op.add_column("discovery_schedule", sa.Column("timezone", sa.Text))
    op.create_check_constraint(
        "ck_discovery_schedule_timed", "discovery_schedule",
        "run_at IS NULL OR (timezone IS NOT NULL AND cardinality(days) >= 1"
        " AND days <@ ARRAY[1,2,3,4,5,6,7]::smallint[])")


def downgrade() -> None:
    op.drop_constraint("ck_discovery_schedule_timed", "discovery_schedule",
                       type_="check")
    for col in ("timezone", "days", "run_at"):
        op.drop_column("discovery_schedule", col)
