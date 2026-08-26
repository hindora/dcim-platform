"""Fold the band pairs that were already open.

Revision ID: 0032
Revises: 0031

A warning rule and a critical rule on ONE measurement fire together: 93 C
crosses `cpu_temp_high` (>80) and `cpu_temp_critical` (>90) at the same moment.
Both rows were shown, so three injected faults on one server produced five
alarms - two acknowledgements and two clears for one hot CPU.

The raise paths collapse bands now. These are the rows that were open when that
landed, and nothing will fold them on its own: the collapse happens when an
alarm is raised, and a standing alarm is not raised again.

Only the LOWER band of a pair is touched, and only where the higher one is open
on the same device, the same metric and the same instance. Both rows stay - the
lower condition is genuinely true - and the lower gains `is_symptom` and a
pointer to the higher, which is what keeps it off the console until the higher
clears and `release_symptoms` lets it go.

Bands are ranked by THRESHOLD, in the direction the rule fires, never by
severity: severity is a label somebody chose and two rules can share one.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


FOLD = sa.text("""
    WITH pair AS (
        SELECT low.id AS low_id, high.id AS high_id,
               low.device_id, low.severity AS low_sev,
               low.alarm_type AS low_type, high.alarm_type AS high_type
          FROM alarm low
          JOIN alarm_rule lr ON lr.alarm_type = low.alarm_type
                            AND lr.metric_key IS NOT NULL
          JOIN alarm_rule hr ON hr.metric_key = lr.metric_key
                            AND hr.alarm_type <> lr.alarm_type
                            AND hr.enabled
                            AND hr.threshold IS NOT NULL
                            AND lr.threshold IS NOT NULL
                            AND ((lr.operator = '>' AND hr.threshold > lr.threshold)
                              OR (lr.operator = '<' AND hr.threshold < lr.threshold))
          JOIN alarm high ON high.alarm_type = hr.alarm_type
                         AND high.device_id = low.device_id
                         AND high.instance IS NOT DISTINCT FROM low.instance
                         AND high.state <> 'CLEARED'
                         AND NOT high.is_symptom
         WHERE low.state <> 'CLEARED'
           AND NOT low.is_symptom
    ), logged AS (
        INSERT INTO alarm_history (alarm_id, device_id, action, severity,
                                   actor, detail)
        SELECT low_id, device_id, 'suppressed', low_sev, 'migration:0032',
               jsonb_build_object('root', high_id::text,
                                  'reason', 'lower band of ' || high_type)
          FROM pair
        RETURNING alarm_id
    )
    UPDATE alarm a
       SET is_symptom = true, root_cause_alarm_id = p.high_id
      FROM pair p
     WHERE a.id = p.low_id
""")

# The down path releases only what this could have suppressed: a symptom whose
# root is a band sibling rather than an upstream device.
UNDO = sa.text("""
    UPDATE alarm low
       SET is_symptom = false, root_cause_alarm_id = NULL
      FROM alarm high, alarm_rule lr, alarm_rule hr
     WHERE low.root_cause_alarm_id = high.id
       AND low.state <> 'CLEARED'
       AND lr.alarm_type = low.alarm_type
       AND hr.alarm_type = high.alarm_type
       AND lr.metric_key = hr.metric_key
       AND low.device_id = high.device_id
""")


def upgrade() -> None:
    op.get_bind().execute(FOLD)


def downgrade() -> None:
    op.get_bind().execute(UNDO)
