"""Name a server's SNMP endpoint after the agent that answers it.

Revision ID: 0026
Revises: 0025

Two servers - the bastions, JUMP1-DC1-NR-R1-03 and JUMP1-DC2-NR-R1-03 - sit in
the network room with no production NIC. `snmp_address` falls back to the
management IP for those, and what answers SNMP at a server's management address
is the service processor, not the operating system.

They were imported as `os_agent` on the server profile, so every two minutes
the collector asked an iDRAC for hrStorage, UCD memory and ifTable. Measured,
that controller serves 29 OIDs: the system group and a vendor subtree, no host
MIBs at all. The poll produced nothing, forever, and the platform reported it
as an equipment condition.

This corrects the rows already imported; `app/importer/endpoints.py` stops
creating them. Written as a JOIN against the Redfish endpoint rather than
against two hard-coded names, because "the SNMP address equals the BMC address"
is the actual condition - any bastion added later has the same shape.

The credential is untouched: community is the address on this plane and the
address is not changing, only the name of what lives there.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


FIX = sa.text("""
    UPDATE device_endpoint e
       SET role = 'bmc',
           poll_profile_id = (SELECT id FROM poll_profile WHERE name = 'snmp-bmc-120s'),
           updated_at = now()
     WHERE e.protocol = 'snmp'
       AND e.role = 'os_agent'
       AND EXISTS (
           SELECT 1 FROM device_endpoint r
            WHERE r.device_id = e.device_id
              AND r.protocol = 'redfish'
              AND r.role = 'bmc'
              AND r.address = e.address
       )
""")

# Deliberately NOT symmetric: the down path cannot know which BMC endpoints
# were once mislabelled os_agent, and guessing would rename correctly-derived
# ones. It restores only what this migration could have changed - a server BMC
# endpoint that is the device's ONLY SNMP endpoint, which is the shape the bug
# produced.
UNDO = sa.text("""
    UPDATE device_endpoint e
       SET role = 'os_agent',
           poll_profile_id = (SELECT id FROM poll_profile WHERE name = 'snmp-server-120s'),
           updated_at = now()
     WHERE e.protocol = 'snmp'
       AND e.role = 'bmc'
       AND (SELECT count(*) FROM device_endpoint o
             WHERE o.device_id = e.device_id AND o.protocol = 'snmp') = 1
       AND EXISTS (
           SELECT 1 FROM device_endpoint r
            WHERE r.device_id = e.device_id
              AND r.protocol = 'redfish'
              AND r.role = 'bmc'
              AND r.address = e.address
       )
""")


def upgrade() -> None:
    op.get_bind().execute(FIX)


def downgrade() -> None:
    op.get_bind().execute(UNDO)
