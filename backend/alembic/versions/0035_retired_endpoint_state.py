"""A retired endpoint stops claiming to be offline.

52 gNMI endpoints on firewalls, load balancers and console switches were
retired when the importer narrowed which device types speak gNMI - PAN-OS and
F5 TMOS serve vendor APIs, not gNMI, and an OOB switch usually speaks SNMP and
nothing else. Retiring them was right. Leaving their state rows at OFFLINE was
not: nothing polls a retired endpoint, so whatever it last said freezes there,
and eight days later the device page still showed a fault against gear that is
perfectly healthy.

The damage is not false alarms - the staleness sweep already filters on
`enabled` - it is that a page carrying 52 permanent untrue OFFLINE rows is a
page where a real OFFLINE row is not noticed.

DISABLED rather than deleted, which is the same choice the importer makes about
the endpoint itself: last_success and the poll totals are the record of what
this endpoint did while it was in service, and an endpoint that comes back
should come back with its history.

Revision ID: 0035
Revises: 0034
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


FIX = sa.text("""
    UPDATE endpoint_state es
       SET status = 'DISABLED',
           last_error = NULL,
           last_error_class = NULL,
           consecutive_failures = 0,
           updated_at = now()
      FROM device_endpoint e
     WHERE e.id = es.endpoint_id
       AND NOT e.enabled
       AND es.status <> 'DISABLED'
""")


def upgrade() -> None:
    result = op.get_bind().execute(FIX)
    print(f"    endpoint_state rows marked disabled: {result.rowcount}")


def downgrade() -> None:
    # Deliberately not reversible. The previous value was a stale judgement
    # about liveness, and restoring it would restore the untruth - there is
    # nothing to gain by putting OFFLINE back on an endpoint nobody polls.
    pass
