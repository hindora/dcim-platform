"""Raising a threshold has to retire what no longer qualifies.

0038 moved `cpu_temp_high` from 80 C to 85 C. The alarms already standing were
raised under the old rule and did not go anywhere: two servers at 81 C kept a
warning open, carrying the message their raise had baked in -

    CPU temperature 81.1 C above 80.0 C

- while the rule behind it now says 85. Nothing would have cleared them either.
The clear threshold moved to 80 with the raise threshold, so at 81 C they sat
above their own clear point indefinitely, describing a limit that no longer
exists.

This is the ordinary consequence of re-rationalising a threshold, and the
ordinary answer: an alarm that would not be raised by the rule as it now
stands is not a condition any more. Cleared rather than deleted, with a
cleared_by that says why, because it did happen and the history should keep it.

Scoped to the one alarm type 0038 changed, and to rows below the new line.
A blanket "retire anything under its rule's threshold" would be a much larger
claim - trigger_value is the reading at RAISE time, and for a condition that
has since worsened it says nothing about whether the condition still holds.
"""

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE alarm a
           SET state = 'CLEARED',
               cleared_at = now(),
               cleared_by = 'threshold-raised'
          FROM alarm_rule r
         WHERE r.alarm_type = a.alarm_type
           AND a.alarm_type = 'cpu_temp_high'
           AND a.state <> 'CLEARED'
           AND a.trigger_value IS NOT NULL
           AND a.trigger_value <= r.threshold
    """)


def downgrade() -> None:
    # Re-opening a cleared alarm would invent a condition rather than restore
    # one: the reading that justified it is minutes or hours old by now, and
    # whether it still holds is a question only the next poll can answer.
    pass
