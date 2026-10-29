"""An outlet is metered where the strip has a current transformer behind it.

Per-outlet current is a tier the vendor sells, not a firmware option: a
Metered AP8886 measures its inlet, legs and banks; a Switched AP8953 has a
relay per receptacle and no CT; only a metered-by-outlet unit (Raritan
PX3-4xxx/5xxx, APC AP84xx/AP86xx) can say what one receptacle draws. The
Power page's rack drill shows the receptacles where a strip reports them and
says "banks only" where it cannot - an outlet with no sample is unmetered,
not idle.

One registry key - `outlet_current` (A, instance = outlet number) - synced
into the metric table by 0106's mechanism, so the ingest worker keeps the
samples the Raritan profile now collects instead of dropping them for want
of a row.

Revision ID: 0118
Revises: 0117
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.core.metrics_gen import METRICS

revision = "0118"
down_revision = "0117"
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
        "UPDATE metric SET deprecated_at = now() WHERE key = 'outlet_current'"))
