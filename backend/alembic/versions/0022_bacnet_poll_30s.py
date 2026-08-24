"""Plant BACnet moves from a ten-second to a thirty-second poll.

Third and last of the throughput changes (0021 covers the first two and the
reasoning). After those, production still ran at roughly twice consumption -
405 stream entries produced per 120 s against 200 consumed - and the pipeline
drifted further behind after every worker restart.

Twenty-four energy monitors on a ten-second BACnet poll were 36% of all
telemetry on their own, each carrying per-circuit points. Thirty seconds takes
about 28% out of total production, which is what puts consumption ahead and
lets the backlog drain by itself.

What it costs: plant telemetry refreshes every 30 s instead of 10 s. For the
equipment alarm points that matters least of all - dwell 2 means a fault that
holds is raised within about a minute, against assertions that have been
running twenty seconds. For the cooling model the loop temperatures move on the
order of minutes, so a 30 s view of them is not a worse view.

What it does NOT change: the alarm points are still read, still evaluated, and
still raise. This is cadence, not coverage.

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

_RENAME = sa.text("""
    UPDATE poll_profile SET name = :new, interval_s = :interval
     WHERE name = :old
""")


def upgrade() -> None:
    op.get_bind().execute(_RENAME, {"old": "bacnet-10s", "new": "bacnet-30s",
                                    "interval": 30})


def downgrade() -> None:
    op.get_bind().execute(_RENAME, {"old": "bacnet-30s", "new": "bacnet-10s",
                                    "interval": 10})
