"""A finding queue has a daily budget; DQ-4 leaves power to the process.

`anomaly_budget_day` counts, per detector and UTC day, the new advisory
findings opened and the ones held back over budget (analytics/budget.py). The
sweep reads today's and yesterday's rows to decide whether a new finding may
open and whether a process detector has been over budget two days running.

DQ-4 (spiky sensor) stops judging power_draw. Power follows workload, and a
server's CPU is bursty: its "spikes" are the process doing what it does, not
an instrument glitching - and they were a quarter of the estate's data-quality
findings. DQ-4 exists for measuring instruments (temperatures, humidity,
pressures, flows); an impossible power reading is still DQ-3's to catch.

Revision ID: 0129
Revises: 0128
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "0129"
down_revision = "0128"
branch_labels = None
depends_on = None

#: Read by tests/anomaly_fakes.seed_rows like 0113's tuning.
PARAM_UPDATES = {"DQ-4": {"exclude_metrics": ["power_draw"]}}


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS anomaly_budget_day (
            detector_id text NOT NULL REFERENCES anomaly_detector(id) ON DELETE CASCADE,
            day         date NOT NULL,
            opened      integer NOT NULL DEFAULT 0,
            held        integer NOT NULL DEFAULT 0,
            PRIMARY KEY (detector_id, day)
        )
    """)
    bind = op.get_bind()
    for did, upd in PARAM_UPDATES.items():
        bind.execute(sa.text("""
            UPDATE anomaly_detector SET params = params || CAST(:p AS jsonb),
                   updated_at = now(), updated_by = 'migration:0129'
             WHERE id = :id
        """), {"id": did, "p": json.dumps(upd)})


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS anomaly_budget_day")
    op.execute("UPDATE anomaly_detector SET params = params - 'exclude_metrics' WHERE id = 'DQ-4'")
