"""Re-sync the metric table after the outdoor-air metrics were added.

Revision ID: 0016
Revises: 0015

Two metrics: `outdoor_dry_bulb_temp` and `outdoor_wet_bulb_temp`, read from the
cooling-tower controller, which is where a BMS keeps them - a tower is
controlled to approach wet bulb, so the outdoor sensor is wired to it.

They are deliberately NOT folded into `ambient_temperature`. That key is an
indoor room sensor, and one key carrying both would let a January night outside
drag a data hall's average inlet down with it.

The mechanism is 0004/0008 verbatim: upsert every registry entry, deprecate -
never delete - anything the registry no longer defines. Rerunning 0003 stopped
being an option the moment telemetry rows started referencing metric ids.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.core.metrics_gen import METRICS

revision = "0016"
down_revision = "0015"
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
