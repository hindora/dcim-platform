"""Network SNMP drops to a ten-minute poll.

The pipeline could not keep up once every protocol came online: 400 samples a
second written against roughly 740 produced, the telemetry stream pinned at its
8,000-entry cap with the consumer group 7,800 behind, and Postgres at 296% CPU
on an eight-core host while the ingest worker sat at 43% of one. The database
was the ceiling, so more workers would have made it worse.

Interfaces were 38% of all telemetry - 5,700 of them across switches, OOB
switches and servers. Dropping the four error and discard columns (see
contracts/mappings/snmp/standard.yaml) took out 17.6%; this takes the network
endpoints that carry most of the remaining interfaces from a two-minute poll to
a ten-minute one.

**The trade-off, stated plainly:** link state seen over SNMP now takes up to ten
minutes to notice. That is acceptable HERE because it is not the only path -
switches and routers stream the same state over gNMI, and every device sends a
linkDown trap, both of which arrive in seconds. It is not acceptable everywhere:
a site whose only view of a port is a two-minute SNMP walk should not take this
migration without adding one of the other two.

Endpoints reference the profile by id, so nothing needs re-pointing; the new
interval reaches the collector on its next assignment refresh.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_RENAME = sa.text("""
    UPDATE poll_profile SET name = :new, interval_s = :interval
     WHERE name = :old
""")


def upgrade() -> None:
    op.get_bind().execute(_RENAME, {"old": "snmp-network-120s",
                                    "new": "snmp-network-600s",
                                    "interval": 600})


def downgrade() -> None:
    op.get_bind().execute(_RENAME, {"old": "snmp-network-600s",
                                    "new": "snmp-network-120s",
                                    "interval": 120})
