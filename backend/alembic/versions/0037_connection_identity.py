"""A link is one row, and the table can say so.

The connection table held 17021 rows for a plant with 2566 links. 14455 of
them were surplus: 2065 groups of identical rows, the worst repeated eight
times - once per import run.

The insert asked for `ON CONFLICT DO NOTHING` with no conflict target, so it
relied on whatever unique constraints existed. The only two were partial:

    uq_connection_a_termination  ... WHERE a_termination_type <> 'none'

They constrain a *port*, not a link - one cable per port, which is true and
worth keeping. But a link whose ports could not be resolved stores
`termination_type = 'none'`, falls outside both indexes, conflicts with
nothing, and is inserted again on every run. 16520 of the 17021 rows had no
port on at least one end, so almost the whole table was outside the only thing
protecting it.

This gives the row an identity of its own: same layer, same two devices, same
two terminations is the same link. NULLS NOT DISTINCT (PG15+) is the point -
without it two portless rows compare unequal on the NULL terminations and the
duplicate slips through exactly as before.

Direction is deliberately NOT canonicalised. On the production layer A->B and
B->A would be the same ethernet link, but on the cooling layer they are the
supply and the return - two different pipes carrying opposite flow. Folding
them by sorted device id would silently delete half the hydronic model. Any
direction-blind uniqueness belongs per-layer, if it is ever wanted.
"""

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep the earliest row of each group: it carries the oldest created_at,
    # and anything referencing a connection should follow the one that has
    # been there since the first import.
    op.execute("""
        DELETE FROM connection c
         USING connection keep
         WHERE c.layer = keep.layer
           AND c.a_device_id = keep.a_device_id
           AND c.b_device_id = keep.b_device_id
           AND c.a_termination_type = keep.a_termination_type
           AND c.b_termination_type = keep.b_termination_type
           AND c.a_termination_id IS NOT DISTINCT FROM keep.a_termination_id
           AND c.b_termination_id IS NOT DISTINCT FROM keep.b_termination_id
           AND (c.created_at, c.id) > (keep.created_at, keep.id)
    """)

    op.execute("""
        CREATE UNIQUE INDEX uq_connection_identity ON connection (
            layer, a_device_id, a_termination_type, a_termination_id,
                   b_device_id, b_termination_type, b_termination_id
        ) NULLS NOT DISTINCT
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_connection_identity")
