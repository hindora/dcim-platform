"""A load is measured at the strip when it says nothing about its draw.

Two registry keys. `outlet_active_power` (W, instance = outlet number) is
the strip's own meter on each cord, from a metered-by-outlet unit's outlet
table; the Power page reads it for a load that publishes no draw of its own,
and says so. `psu_input_power` (W, instance = supply) is what a switch's
supplies take in, from openconfig-platform-psu over gNMI; the chassis draw
itself arrives as power_draw from the ENTITY-SENSOR walk that sums them.

Synced into the metric table by 0106's mechanism, so the ingest worker keeps
the samples instead of dropping them for want of a row.

Revision ID: 0119
Revises: 0118
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.core.metrics_gen import METRICS

revision = "0119"
down_revision = "0118"
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
    op.execute(sa.text(
        "UPDATE metric SET deprecated_at = now() "
        "WHERE key IN ('outlet_active_power', 'psu_input_power')"))
