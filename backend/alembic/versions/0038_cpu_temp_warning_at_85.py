"""A busy CPU is not a hot CPU.

`cpu_temp_high` warned above 80 C. A Xeon package under sustained full load
sits in the low 80s with nothing wrong with it - Tjmax is around 100 C and
server vendors set their package warnings in the high 80s - so the rule fired
on healthy, fully loaded hardware.

Seen on this fleet: a CPU-load fault was injected on SRV10-DC1-HB-R2-04 and
nothing else. The operator got a temperature warning they had not asked for.
It was not a false reading - the simulator's own air-cooled die model is

    38.0 + cpu_usage * 0.45  (+ 0.9 per degree of intake above 22 C)

and at cpu_usage 93 with a 23.2 C inlet that predicts 80.93 against a measured
81.01. The number was right. The line was in the wrong place: the model tops
out near 83 C at 100 % load, so any genuinely busy server crossed it.

The simulator draws the same line at 85, and the two disagreeing about what
"hot" means is what produced a warning nobody could act on. 85 also leaves
plenty of room for the faults that SHOULD reach it - the model adds up to
+38 C for a direct-to-chip cold-plate leak and +34 C for a stopped CDU, and
`cpu_temp_critical` still sits at 90.

The clear threshold moves with it, keeping the 5 C hysteresis the rule was
written with. That it now equals `cpu_temp_critical`'s own clear point is
correct and not a collision: passing back down through 85 clears the critical
and leaves the warning standing until 80.
"""

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE alarm_rule
           SET threshold = 85, clear_threshold = 80
         WHERE alarm_type = 'cpu_temp_high'
           AND metric_key = 'cpu_temperature'
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE alarm_rule
           SET threshold = 80, clear_threshold = 75
         WHERE alarm_type = 'cpu_temp_high'
           AND metric_key = 'cpu_temperature'
    """)
