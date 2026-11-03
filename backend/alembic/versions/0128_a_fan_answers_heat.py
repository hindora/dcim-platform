"""SV-2 judges a fan against the heat it answers, not against CPU load.

A server's BMC drives its fans from component temperatures - the CPUs, the
DIMMs - and the intake. Fitted on CPU load and intake (0127), a dried-out
thermal interface made the fans ramp with a hotter die, and SV-2 would have
reported all four fans beside SV-1's one finding for the same fault. On CPU
temperature and intake the ramp is explained, and a worn fan - faster than
both explain - is still found. analytics/l3_specs.py carries the drivers;
this updates the row's required points and wording to match.

Revision ID: 0128
Revises: 0127
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0128"
down_revision = "0127"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(sa.text("""
        UPDATE anomaly_detector
           SET required_metrics = ARRAY['fan_speed', 'cpu_temperature', 'inlet_temperature'],
               diagnosis = 'A fan spinning faster than this server''s CPU temperature and '
                           'intake call for: a blocked intake, a worn bearing, or the BMC '
                           'covering for a weak neighbouring fan',
               updated_at = now(), updated_by = 'migration:0128'
         WHERE id = 'SV-2'
    """))


def downgrade() -> None:
    op.get_bind().execute(sa.text("""
        UPDATE anomaly_detector
           SET required_metrics = ARRAY['fan_speed', 'inlet_temperature', 'cpu_utilization'],
               updated_by = 'migration:0128 downgrade'
         WHERE id = 'SV-2'
    """))
