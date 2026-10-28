"""A tripped branch breaker is polled too, not only trapped.

A branch breaker that opens kills the outlets behind it: a dual-corded load
rides it on the other strip, a single-corded one goes dark. The strip says so
with a trap (Raritan overCurrentProtectorSensorStateChange, naming the
protector), and a trap is the accelerator, never the record: a lost one meant
nobody knew. PDU2-MIB also publishes each protector's trip sensor, so the state
can be read on every poll.

`bank_breaker_closed` (bool, instance = the breaker's label, "C1") and a
`breaker_tripped` rule on it. Same alarm_type and instance as the trap, so the
trap and the poll maintain ONE row: the trap raises it at once, the poll raises
it if the trap was lost and closes it when the breaker reads closed again -
which the reset trap may also do. APC strips publish no breaker object
(PowerNet has none), so for them the trap and its reset are all there is.

Revision ID: 0117
Revises: 0116
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.core.metrics_gen import METRICS

revision = "0117"
down_revision = "0116"
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

_RULE = sa.text("""
    INSERT INTO alarm_rule (name, alarm_type, metric_key, severity,
                            dwell_samples, clear_dwell_samples, device_types,
                            message_tpl, enabled, metric_kind, raise_on,
                            instances, detection)
    VALUES ('bank-breaker-tripped', 'breaker_tripped', 'bank_breaker_closed',
            CAST('CRITICAL' AS severity_t), 1, 2, '{}', :message_tpl, true,
            'boolean', false, '{}', 'state')
    ON CONFLICT (name) DO UPDATE SET
        alarm_type = EXCLUDED.alarm_type,
        metric_key = EXCLUDED.metric_key,
        severity = EXCLUDED.severity,
        dwell_samples = EXCLUDED.dwell_samples,
        clear_dwell_samples = EXCLUDED.clear_dwell_samples,
        message_tpl = EXCLUDED.message_tpl,
        metric_kind = EXCLUDED.metric_kind,
        raise_on = EXCLUDED.raise_on,
        instances = EXCLUDED.instances,
        detection = EXCLUDED.detection
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
    conn.execute(_RULE, {
        "message_tpl": "Branch breaker {point} is open: the outlets on it are dead, "
                       "and the loads corded there are running on their other "
                       "feed or are down",
    })


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM alarm_rule WHERE name = 'bank-breaker-tripped'"))
    op.execute(sa.text(
        "UPDATE metric SET deprecated_at = now() WHERE key = 'bank_breaker_closed'"))
