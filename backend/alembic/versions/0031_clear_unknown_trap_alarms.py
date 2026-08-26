"""Clear the alarms that unmapped traps left behind.

Revision ID: 0031
Revises: 0030

An unmapped trap used to raise an alarm. There is no rule behind an unmapped
OID and no recovery trap that names one, so every row it created is open
forever: nothing in the platform can resolve `unknown_trap`, and nothing on the
equipment will ever send a clear for it.

The raise path is fixed - an unmapped OID is now recorded as an event and
nothing else - and the trap mapping is regenerated from the transmit path, so
the OIDs that produced these are mapped. This clears what the old behaviour
left standing, with history saying why.

The EVENTS stay. They are the record of which OIDs this platform could not
resolve, and that record is the reason the mapping got fixed.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


CLEAR = sa.text("""
    WITH stuck AS (
        SELECT id, device_id, severity FROM alarm
         WHERE alarm_type = 'unknown_trap'
           AND state <> 'CLEARED'
    ), logged AS (
        INSERT INTO alarm_history (alarm_id, device_id, action, severity,
                                   actor, detail)
        SELECT id, device_id, 'clear', severity, 'migration:0031',
               '{"reason": "unmapped traps no longer raise alarms; the OID is mapped now"}'
        FROM stuck
        RETURNING alarm_id
    )
    UPDATE alarm SET state = 'CLEARED', cleared_at = now()
     WHERE id IN (SELECT id FROM stuck)
""")


def upgrade() -> None:
    op.get_bind().execute(CLEAR)


def downgrade() -> None:
    # Nothing to restore. These alarms should not have existed, and re-opening
    # them would recreate rows the platform has no way to clear.
    pass
