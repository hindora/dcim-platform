"""An anomaly is shown on the device, not raised as an alarm - with three exceptions.

A finding is a diagnosis ("this looks wrong, and here is probably why"), not a
demand for action within a set time, which is what ISA-18.2 / EEMUA 191 call an
alarm. Raised as alarms, the rule detectors turned one CRAH supply-temperature
fault into three entries on the console: the unit's own alarm and two derived
ones, one of them (low delta-T, "bypass air") naming the wrong cause.

So the rule detectors move to the advisory lane: their findings stay open and
are shown in the 3D view's device panel and as a marker on the unit, and raise
nothing. Three stay alarms, because each catches a fault the equipment often
does not report itself and that needs a response now:

* PU-3 pump failed to prove - commanded on, no flow proven (a standard BMS alarm);
* GN-1 generator fuel loss - fuel falling while the set is not running;
* CD-6 CDU condensation risk - water forming on live IT equipment.

Any detector can be moved back from Settings > Anomaly detectors. Derived
alarms already open for the moved detectors are cleared by the next sweep
(`_sync_lanes`), with a history entry saying why; their findings stay open.

Revision ID: 0124
Revises: 0123
"""

from __future__ import annotations

from alembic import op

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None

KEEP_ALERT = ("PU-3", "GN-1", "CD-6")
MOVED = ("CD-1", "CH-1", "CH-4", "CH-5", "CR-1", "CR-2", "CR-6", "RM-2", "RM-3",
         "VA-1", "VA-2")


def upgrade() -> None:
    ids = ", ".join(f"'{i}'" for i in MOVED)
    op.execute(f"""
        UPDATE anomaly_detector
           SET default_lane = 'advisory', updated_at = now(),
               updated_by = 'migration 0124'
         WHERE id IN ({ids}) AND default_lane = 'alert'
    """)


def downgrade() -> None:
    ids = ", ".join(f"'{i}'" for i in MOVED)
    op.execute(f"""
        UPDATE anomaly_detector SET default_lane = 'alert', updated_at = now()
         WHERE id IN ({ids})
    """)
