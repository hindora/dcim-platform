"""Dew point high waits no longer than dew point critical.

0132 gave dew-point-high a dwell of 3 samples and dew-point-critical 2. A
lower band is a superset of the higher one, so the WARNING landed after its
own CRITICAL and the rule loader said so on every start ("band dwells
contradict each other"). The lower band now waits 2, the same as the
higher; 0132's seed is corrected for fresh installs.

Revision ID: 0133
Revises: 0132
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0133"
down_revision = "0132"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(sa.text(
        "UPDATE alarm_rule SET dwell_samples = 2 WHERE name = 'dew-point-high'"))


def downgrade() -> None:
    op.get_bind().execute(sa.text(
        "UPDATE alarm_rule SET dwell_samples = 3 WHERE name = 'dew-point-high'"))
