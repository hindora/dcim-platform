"""A pool's default credential can be scoped to device types.

A site moves a management network to SNMPv3 by device class. On the IT-OOB
networks the switches, routers, firewalls and load balancers took the pool's v3
default; the server BMCs beside them on the same subnets stay v2c. The default
said nothing about that, so:

- a NEW BMC imported into the pool got no credential of its own (the importer
  leaves endpoints in a pool with a default to that default) and was then
  served the v3 user, which it does not have - polling failed from day one;
- an adopt with no device types would have done the same to every BMC.

device_types NULL keeps today's meaning: the default covers every device in
the pool. A list narrows it; an endpoint of another type with no credential of
its own is served none, rather than the wrong one, and the importer pins its
per-device credential instead.

Revision ID: 0098
Revises: 0097
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("collector_pool_credential",
                  sa.Column("device_types", postgresql.ARRAY(sa.Text), nullable=True))


def downgrade() -> None:
    op.drop_column("collector_pool_credential", "device_types")
