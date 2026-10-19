"""A CDU's loop is current state: return water, flow and the heat it carries.

Only hot metrics reach device_state.metrics, which is what the room viewer's
device panel and the twin read. A coolant distribution unit reported its
return water temperature, its loop flow and its thermal load on every poll
and all three went to the time series alone, so the rack view showed a CDU
with a supply temperature and no loop: no return, no flow, no rise. The
registry now marks water_return_temp, water_flow and thermal_load hot, and
the metric table is re-synced from it (0103's mechanism verbatim) so the
ingest worker promotes them.

Revision ID: 0106
Revises: 0105
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.core.metrics_gen import METRICS

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None

UPSERT_METRIC = sa.text("""
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
    conn.execute(UPSERT_METRIC, [
        {"key": m.key, "display_name": m.display_name, "unit": m.unit,
         "value_type": m.value_type, "aggregation": m.aggregation,
         "min_valid": m.min_valid, "max_valid": m.max_valid,
         "stale_after_s": m.stale_after_s, "is_hot": m.hot}
        for m in METRICS.values()
    ])


def downgrade() -> None:
    op.execute(sa.text("""
        UPDATE metric SET is_hot = false
        WHERE key IN ('water_return_temp', 'water_flow', 'thermal_load')
    """))
