"""Re-sync the metric table after the machine temperatures were added.

Revision ID: 0062
Revises: 0061

Three keys, and the reason they are three rather than one is the whole point.

  battery_temperature   a UPS string's own temperature. VRLA capacity and
                        service life halve for roughly each 10 K above the
                        25 C the cells are specified at, so this is what
                        decides when a string is replaced and what catches
                        thermal runaway while it is still a temperature.
  coolant_temperature   engine coolant. A standby genset reads its jacket
                        heater, held near 40 C so the set can take load ten
                        seconds after a utility failure (NFPA 110), and
                        85-95 C under load.

Neither is ambient_temperature, and neither may be folded into it. A battery
runs warmer than the room it stands in by design and a jacket heater warmer
still, so a room that took either as its air would read a 24 C switchroom as
26 C on a quiet day and as 33 C during an outage in which the room never moved.
The third key is the one that CAN answer for those rooms: the room-air
transmitters now fitted in the UPS and generator halls report
`ambient_temperature`, the same key a rack probe reports, because it is the
same quantity measured in a room that has no rack to put a probe in.

The mechanism is 0004/0008/0016 verbatim: upsert every registry entry,
deprecate - never delete - anything the registry no longer defines. Rerunning
0003 stopped being an option the moment telemetry rows started referencing
metric ids.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.core.metrics_gen import METRICS

revision = "0062"
down_revision = "0061"
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
