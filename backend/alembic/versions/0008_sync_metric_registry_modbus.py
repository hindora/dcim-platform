"""Re-sync the metric table after the Modbus power metrics were added.

Revision ID: 0008
Revises: 0007

Phase 3.5 added 14 electrical metrics to the registry for the Modbus adapter -
line-to-line voltage, reactive and apparent power, load, imbalance, battery and
fuel state, transfer counters, and the operating_mode state word. The mechanism
is 0004 verbatim, with a new revision id.

Adding a metric to the registry has to reach the database, and rerunning 0003
is not an option once telemetry rows reference metric ids. This migration is
the reusable mechanism: it upserts every registry entry and deprecates - never
deletes - anything the registry no longer defines.

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.core.metrics_gen import METRICS

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

UPSERT = sa.text("""
    INSERT INTO metric (key, display_name, unit, value_type, aggregation,
                        min_valid, max_valid, stale_after_s, is_hot)
    VALUES (:key, :display_name, :unit, :value_type, :aggregation,
            :min_valid, :max_valid, :stale_after_s, :is_hot)
    ON CONFLICT (key) DO UPDATE
      SET display_name = EXCLUDED.display_name,
          unit = EXCLUDED.unit,
          value_type = EXCLUDED.value_type,
          aggregation = EXCLUDED.aggregation,
          min_valid = EXCLUDED.min_valid,
          max_valid = EXCLUDED.max_valid,
          stale_after_s = EXCLUDED.stale_after_s,
          is_hot = EXCLUDED.is_hot,
          deprecated_at = NULL
""")


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(UPSERT, [
        {"key": m.key, "display_name": m.display_name, "unit": m.unit,
         "value_type": m.value_type, "aggregation": m.aggregation,
         "min_valid": m.min_valid, "max_valid": m.max_valid,
         "stale_after_s": m.stale_after_s, "is_hot": m.hot}
        for m in METRICS.values()
    ])
    # Deprecated, not deleted: hypertable rows still reference the id.
    conn.execute(sa.text("""
        UPDATE metric SET deprecated_at = now()
        WHERE deprecated_at IS NULL AND key <> ALL(:keys)
    """), {"keys": list(METRICS.keys())})


def downgrade() -> None:
    # A registry sync has no meaningful inverse: the previous contents are not
    # recoverable from this file, and dropping metrics would orphan telemetry.
    pass
