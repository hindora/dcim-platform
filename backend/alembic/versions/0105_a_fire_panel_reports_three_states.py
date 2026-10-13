"""A fire panel reports three states, and a DCIM reads all of them.

The simulator's fire system (its docs/FIRE_SYSTEM.md): a fire alarm control
panel per site, and in each hall an aspirating smoke detector and a
clean-agent release panel, on the BMS trunk. A fire system is its own listed
system and a DCIM only ever reads a one-way feed from it, so everything here
is a reading and a rule - nothing commands anything.

The metric table is re-synced from the registry (0103's mechanism verbatim)
so the ingest worker accepts the new keys, and the rules are seeded. The
numbers are the equipment's, not ours:

  smoke stage   the detector's own stages, not a threshold of ours on its
                obscuration: Alert (1) minor - go and look; Action (2) major -
                something is burning or about to; Fire 1 (3) critical.
  panel alarm   critical, no dwell. Nobody debounces a fire alarm.
  trouble       major after a minute: the system cannot vouch for itself.
  supervisory   major after a minute: something that protects is off normal.
  release       state above first alarm is the pre-discharge countdown, the
                abort hold or the discharge itself - critical: leave the room.
                Agent released is its own critical, and it does not clear by
                itself: the room is unprotected until the bank is replaced.
  cylinders     below 2250 kPa on a 2500 kPa bank, major: a bank that has
                lost a tenth of its charge will not make its concentration.

Every rule is scoped to `sensor`.

Revision ID: 0105
Revises: 0104
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.core.metrics_gen import METRICS

revision = "0105"
down_revision = "0104"
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

# name, alarm_type, metric, operator, threshold, clear, severity, message
THRESHOLD_RULES = [
    ("smoke-alert", "smoke_alert", "smoke_stage", ">", 0.5, 0.5, "MINOR",
     "Aspirating detector at Alert - investigate the room"),
    ("smoke-action", "smoke_action", "smoke_stage", ">", 1.5, 1.5, "MAJOR",
     "Aspirating detector at Action - smoke confirmed"),
    ("smoke-fire", "smoke_fire", "smoke_stage", ">", 2.5, 2.5, "CRITICAL",
     "Aspirating detector at Fire - follow the fire plan"),
    ("gas-release-imminent", "gas_release_imminent", "gas_release_state", ">", 1.5, 1.5, "CRITICAL",
     "Gas release sequence active - leave the room"),
    ("gas-agent-pressure-low", "gas_agent_pressure_low", "agent_pressure", "<", 2250.0, 2350.0, "MAJOR",
     "Suppression cylinders at {value} kPa, below {threshold} kPa"),
]

# name, alarm_type, metric, dwell_seconds, severity, message
STATE_RULES = [
    ("fire-alarm", "fire_alarm_active", "fire_alarm", 0, "CRITICAL", "Fire alarm"),
    ("fire-panel-trouble", "fire_panel_trouble", "fire_trouble", 60, "MAJOR",
     "Fire panel in trouble - detection impaired"),
    ("fire-panel-supervisory", "fire_panel_supervisory", "fire_supervisory", 60, "MAJOR",
     "Fire panel supervisory - protection off normal"),
    ("gas-agent-released", "gas_agent_released", "agent_released", 0, "CRITICAL",
     "Suppression agent released - room unprotected"),
]

UPSERT_THRESHOLD = sa.text("""
    INSERT INTO alarm_rule (
        name, alarm_type, enabled, device_types, metric_key, metric_kind,
        operator, threshold, clear_threshold, dwell_samples,
        clear_dwell_samples, severity, message_tpl, detection)
    VALUES (
        :name, :alarm_type, true, ARRAY['sensor'], :metric, 'numeric',
        :op, :threshold, :clear_threshold, 1, 2,
        CAST(:severity AS severity_t), :message, 'threshold')
    ON CONFLICT (name) DO UPDATE SET
        enabled             = EXCLUDED.enabled,
        alarm_type          = EXCLUDED.alarm_type,
        device_types        = EXCLUDED.device_types,
        metric_key          = EXCLUDED.metric_key,
        metric_kind         = EXCLUDED.metric_kind,
        operator            = EXCLUDED.operator,
        threshold           = EXCLUDED.threshold,
        clear_threshold     = EXCLUDED.clear_threshold,
        dwell_samples       = EXCLUDED.dwell_samples,
        clear_dwell_samples = EXCLUDED.clear_dwell_samples,
        severity            = EXCLUDED.severity,
        message_tpl         = EXCLUDED.message_tpl,
        detection           = EXCLUDED.detection
""")

UPSERT_STATE = sa.text("""
    INSERT INTO alarm_rule (
        name, alarm_type, enabled, device_types, metric_key, metric_kind,
        raise_on, dwell_samples, dwell_seconds, clear_dwell_samples,
        severity, message_tpl, detection)
    VALUES (
        :name, :alarm_type, true, ARRAY['sensor'], :metric, 'boolean',
        true, 1, :dwell_seconds, 2, CAST(:severity AS severity_t), :message, 'state')
    ON CONFLICT (name) DO UPDATE SET
        enabled             = EXCLUDED.enabled,
        alarm_type          = EXCLUDED.alarm_type,
        device_types        = EXCLUDED.device_types,
        metric_key          = EXCLUDED.metric_key,
        metric_kind         = EXCLUDED.metric_kind,
        raise_on            = EXCLUDED.raise_on,
        dwell_samples       = EXCLUDED.dwell_samples,
        dwell_seconds       = EXCLUDED.dwell_seconds,
        clear_dwell_samples = EXCLUDED.clear_dwell_samples,
        severity            = EXCLUDED.severity,
        message_tpl         = EXCLUDED.message_tpl,
        detection           = EXCLUDED.detection
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
    conn.execute(sa.text("""
        UPDATE metric SET deprecated_at = now()
        WHERE deprecated_at IS NULL AND key <> ALL(:keys)
    """), {"keys": list(METRICS.keys())})
    for name, alarm_type, metric, op_, thr, clear, sev, msg in THRESHOLD_RULES:
        op.execute(UPSERT_THRESHOLD.bindparams(
            name=name, alarm_type=alarm_type, metric=metric, op=op_,
            threshold=thr, clear_threshold=clear, severity=sev, message=msg))
    for name, alarm_type, metric, dwell, sev, msg in STATE_RULES:
        op.execute(UPSERT_STATE.bindparams(
            name=name, alarm_type=alarm_type, metric=metric, dwell_seconds=dwell,
            severity=sev, message=msg))


def downgrade() -> None:
    names = [r[0] for r in THRESHOLD_RULES] + [r[0] for r in STATE_RULES]
    op.execute(sa.text("DELETE FROM alarm_rule WHERE name = ANY(:names)").bindparams(names=names))
    # The registry sync has no inverse: dropping metrics would orphan telemetry.
