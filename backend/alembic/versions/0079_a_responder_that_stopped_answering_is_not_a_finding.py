"""A candidate whose address went silent stayed in the queue for ever.

Nothing aged a responder out. The discovery page says "something is answering that
no record accounts for", and it said that about a machine which had not answered
since a sweep hours earlier - while a later sweep of the same subnet completed and
never saw it. A false positive on an audit page is the failure that page exists to
prevent.

`last_seen` was recorded and nothing acted on it. A run knows which subnets it
swept, so it can tell the difference between "not asked" and "asked and silent":
a candidate inside the swept scope that this run did not see has gone, and one
outside the scope is simply unknown and must be left alone. Marking rather than
deleting, because a responder that used to answer at an address is itself worth
keeping - it is how somebody later works out what used to be there.

This migration only widens the open-candidate index. `status` is free text with no
constraint, so 'gone' needs no type change.

The index is the part that matters. It was UNIQUE on (address, protocol) WHERE
status = 'new', so once a row went to 'gone' the upsert's ON CONFLICT no longer saw
it and a device that came back would INSERT a SECOND row for the same address -
turning an aged-out responder into a duplicate the moment it answered again. Both
states now share the index, and the upsert resurrects a 'gone' row to 'new' rather
than inserting beside it.

Revision ID: 0079
Revises: 0078
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_discovery_candidate_open", table_name="discovery_candidate")
    # One row per address and protocol across BOTH open states. An ignored or
    # promoted candidate is deliberately outside the index: dismissing a responder
    # and later rediscovering it should stage a fresh one rather than silently
    # reopening the decision somebody already made.
    op.create_index("uq_discovery_candidate_open", "discovery_candidate",
                    ["address", "protocol"], unique=True,
                    postgresql_where=sa.text("status IN ('new', 'gone')"))

    # Marking gone reads (status, address) for every candidate inside a swept
    # scope. Without this it is a full scan of the table on every run.
    op.create_index("ix_discovery_candidate_open_address", "discovery_candidate",
                    ["address"],
                    postgresql_where=sa.text("status IN ('new', 'gone')"))


def downgrade() -> None:
    op.drop_index("ix_discovery_candidate_open_address",
                  table_name="discovery_candidate")
    op.drop_index("uq_discovery_candidate_open", table_name="discovery_candidate")
    # Narrowing the index can fail where two rows now share an address across the
    # two states, which is exactly the duplicate this migration prevents. Anything
    # still marked gone is folded back to new first so the old index can build.
    op.execute("UPDATE discovery_candidate SET status = 'new' WHERE status = 'gone'")
    op.create_index("uq_discovery_candidate_open", "discovery_candidate",
                    ["address", "protocol"], unique=True,
                    postgresql_where=sa.text("status = 'new'"))
