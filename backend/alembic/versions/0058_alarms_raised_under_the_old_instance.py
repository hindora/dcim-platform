"""Re-file open alarms that a device-scoped type raised under a point name.

Revision ID: 0058
Revises: 0057

0056 made plant_unit_stopped file at the MACHINE rather than at the point that
revealed it, so a trap and a polled rule would stop producing two rows for one
stopped unit. Alarms raised before that change carry the old instance, and the
alarm key is (device, alarm_type, instance) - so the rule that would clear one
of them now looks under the new key, finds nothing, and the row stays ACTIVE
with the machine running again.

That is exactly what happened: three CRAHs raised at 13:14 under
`Unit_Running`, restarted by the operator minutes later, and still ACTIVE on
the console while the same page showed them running with an OK verdict.

Re-files them under the key the current code uses. Only OPEN alarms, and only
the types that are device-scoped today: a cleared row is history and should
keep the shape it had when it was written.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.core.alert_taxonomy import DEVICE_SCOPED_ALARM_TYPES

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    types = sorted(DEVICE_SCOPED_ALARM_TYPES)
    if not types:
        return
    op.execute(sa.text("""
        UPDATE alarm
           SET instance = ''
         WHERE state <> 'CLEARED'
           AND instance <> ''
           AND alarm_type = ANY(:types)
    """).bindparams(types=types))


def downgrade() -> None:
    # Not reversible, and it should not be: the instance those rows carried was
    # the point name, which the row no longer records. Leaving them at the
    # device is also the shape the running code expects, so a downgrade that
    # invented the old value back would break the same clear a second time.
    pass
