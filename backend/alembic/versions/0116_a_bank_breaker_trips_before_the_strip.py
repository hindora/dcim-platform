"""A bank breaker trips before the strip does: bank current and its rating.

A rack strip's outlets are wired in banks, each behind its own breaker - a
32 A AP8886 is six 16 A banks, two on each leg. The platform read the strip's
total and its three legs, and a leg at 60 % can be one bank at 95 % and the
other nearly empty: the server that goes into that bank trips it, and the
strip's own figures never said so.

Two registry keys - `bank_current` (A, instance = bank) and
`bank_breaker_rating` (A, instance = bank, only where breakers are fitted) -
synced into the metric table by 0106's mechanism, so the ingest worker keeps
their samples instead of dropping them for want of a row.

Revision ID: 0116
Revises: 0115
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.core.metrics_gen import METRICS

revision = "0116"
down_revision = "0115"
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
        "WHERE key IN ('bank_current', 'bank_breaker_rating')"))
