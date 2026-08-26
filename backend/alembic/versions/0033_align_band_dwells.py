"""A lower band must not wait longer than the higher one above it.

Revision ID: 0033
Revises: 0032

`cpu_temp_high` (>80) needed 3 samples of dwell while `cpu_temp_critical`
(>90) needed 2, so a CPU going straight past both raised the CRITICAL a full
poll before its own WARNING. An operator reading the list top-down watched the
situation apparently improve while nothing had changed. Same shape on
`inlet_temperature`.

It is not a tuning preference, it is incoherent: a lower band is a SUPERSET of
a higher one. A value that has been above 90 for three samples has been above
80 for at least three. Requiring MORE evidence for the weaker claim cannot be
satisfied earlier than the stronger one, so the ordering is guaranteed
backwards whenever a reading crosses both at once.

Each rule's dwell becomes the smallest among itself and every HIGHER band on
the same metric in the same direction. That preserves the thing the differing
dwells were reaching for - a critical may react faster than a warning, and
still does - while removing the case that cannot happen in nature.

Not touched: two rules on one metric with OPPOSITE operators. `humidity_low`
(<20) and `humidity_high` (>70) are not bands of each other, they are two ends
of a range, and neither is a superset of anything.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


# dwell_samples: the minimum across this rule and every higher band.
ALIGN_SAMPLES = sa.text("""
    WITH family AS (
        SELECT low.id,
               min(coalesce(high.dwell_samples, 0)) AS cap
          FROM alarm_rule low
          JOIN alarm_rule high
            ON high.metric_key = low.metric_key
           AND high.operator = low.operator
           AND high.enabled
           AND high.threshold IS NOT NULL
           AND ((low.operator = '>' AND high.threshold > low.threshold)
             OR (low.operator = '<' AND high.threshold < low.threshold))
         WHERE low.enabled
           AND low.metric_key IS NOT NULL
           AND low.threshold IS NOT NULL
         GROUP BY low.id
    )
    UPDATE alarm_rule r
       SET dwell_samples = LEAST(r.dwell_samples, f.cap)
      FROM family f
     WHERE r.id = f.id
       AND r.dwell_samples > f.cap
""")

# dwell_seconds is a second, independent gate - both must be satisfied - so it
# needs the same treatment. NULL means "no time requirement", which is the
# weakest of all: a higher band with no time gate caps the lower at none.
ALIGN_SECONDS = sa.text("""
    WITH family AS (
        SELECT low.id,
               bool_or(high.dwell_seconds IS NULL) AS any_unbounded,
               min(high.dwell_seconds)             AS cap
          FROM alarm_rule low
          JOIN alarm_rule high
            ON high.metric_key = low.metric_key
           AND high.operator = low.operator
           AND high.enabled
           AND high.threshold IS NOT NULL
           AND ((low.operator = '>' AND high.threshold > low.threshold)
             OR (low.operator = '<' AND high.threshold < low.threshold))
         WHERE low.enabled
           AND low.metric_key IS NOT NULL
           AND low.threshold IS NOT NULL
           AND low.dwell_seconds IS NOT NULL
         GROUP BY low.id
    )
    UPDATE alarm_rule r
       SET dwell_seconds = CASE WHEN f.any_unbounded THEN NULL
                                ELSE LEAST(r.dwell_seconds, f.cap) END
      FROM family f
     WHERE r.id = f.id
       AND (f.any_unbounded OR r.dwell_seconds > f.cap)
""")


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(ALIGN_SAMPLES)
    bind.execute(ALIGN_SECONDS)


def downgrade() -> None:
    # The original per-rule dwells are not recoverable from the aligned ones -
    # several distinct values collapse to one - and restoring them would put
    # back a state that raises a critical before its own warning. 0006 and 0025
    # hold the seeded values for anyone who genuinely wants them.
    pass
