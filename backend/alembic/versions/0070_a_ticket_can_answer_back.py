"""The inbound half: what Jira tells us, and which condition it is about.

Revision ID: 0070
Revises: 0069

TWO CHANGES, AND THE SECOND IS THE INTERESTING ONE.

**`integration_inbox`** is the mirror of the outbox, for the same reason. A
Jira webhook gives the receiver about 30 seconds and retries up to five times
on anything that is not a 2xx, with a randomised 5-15 minute backoff. So the
handler must verify the signature, write ONE row, and return - no correlation,
no alarm mutation, no outbound call. Everything that could be slow or could
fail happens on the dispatcher, where a failure is a retry rather than a
redelivery.

`dedup_sha` is how a redelivery stays harmless. Jira sends no delivery id of
its own, so the key is a digest of the exact bytes received: the same body
twice is the same event twice, and the unique index drops the second. That is
weaker than a real idempotency key - two genuinely distinct events with
identical bodies would collide - but a Jira payload carries a millisecond
`timestamp`, so identical bytes really do mean a redelivery.

**`jira_link` learns the condition's identity.** The link already knew which
ALARM ROW it was opened for, and that is not the same question. A condition
clears at 02:00 and raises again at 06:00 as a new row with a new uuid; the
ticket is about the condition, not the row. When an engineer closes that
ticket at 09:00, the alarm that should be acknowledged is the one open NOW,
not the one that happened to exist when the ticket was created.

So the link carries `(device_id, alarm_type, instance)` - the same tuple the
fingerprint is built from and the same one `alarm_active_key` indexes - and
the inbound path resolves through it. `alarm_id` stays as the row it last
pushed for, which is what the console's chip wants, and is allowed to go
stale.
"""

from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE integration_inbox (
            id             bigserial PRIMARY KEY,
            integration_id uuid NOT NULL
                           REFERENCES integration(id) ON DELETE CASCADE,
            -- jira:issue_updated | jira:issue_deleted | comment_created
            event          text NOT NULL,
            issue_key      text,
            payload        jsonb NOT NULL,
            -- sha256 of the exact bytes received. See the module docstring.
            dedup_sha      text NOT NULL,

            received_at    timestamptz NOT NULL DEFAULT now(),
            state          text NOT NULL DEFAULT 'pending',
            attempts       smallint NOT NULL DEFAULT 0,
            claimed_by     text,
            claimed_at     timestamptz,
            last_error     text,

            CONSTRAINT ck_inbox_state
                CHECK (state IN ('pending','done','dead'))
        )
    """)

    # The redelivery guard. UNIQUE rather than a check-then-insert, because
    # two workers can be handed the same retry at the same moment and only the
    # database can settle that.
    op.execute("CREATE UNIQUE INDEX ix_inbox_dedup ON integration_inbox (dedup_sha)")
    # The dispatcher's claim: oldest first, because an issue's events are only
    # meaningful in order - a reopen followed by a close is not a close
    # followed by a reopen.
    op.execute("""
        CREATE INDEX ix_inbox_pending ON integration_inbox (id)
         WHERE state = 'pending'
    """)
    op.execute("""
        CREATE INDEX ix_inbox_dead ON integration_inbox (integration_id, id DESC)
         WHERE state = 'dead'
    """)

    # ------------------------------------------------- the link's identity
    op.execute("ALTER TABLE jira_link ADD COLUMN alarm_type text")
    op.execute("ALTER TABLE jira_link ADD COLUMN instance text NOT NULL DEFAULT ''")

    # Backfill from whatever row the link last pushed for. Best effort by
    # definition - a link whose alarm has already been purged cannot be
    # recovered - and those resolve to NULL, where the inbound path falls back
    # to `alarm_id` rather than guessing.
    op.execute("""
        UPDATE jira_link l
           SET alarm_type = a.alarm_type,
               instance   = a.instance
          FROM alarm a
         WHERE a.id = l.alarm_id AND l.alarm_type IS NULL
    """)

    # "Which ticket is this condition's", asked by the outbound path when a
    # link's fingerprint is not to hand.
    op.execute("""
        CREATE INDEX ix_jira_link_condition
            ON jira_link (device_id, alarm_type, instance)
         WHERE alarm_type IS NOT NULL
    """)

    # What the reopen decision reads back from Jira, and the webhook writes.
    # Recorded here rather than inferred, so "the ticket was closed as Won't
    # Fix" survives a later reopen-and-reclose.
    op.execute("ALTER TABLE jira_link ADD COLUMN last_inbound_at timestamptz")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_jira_link_condition")
    op.execute("ALTER TABLE jira_link DROP COLUMN IF EXISTS last_inbound_at")
    op.execute("ALTER TABLE jira_link DROP COLUMN IF EXISTS instance")
    op.execute("ALTER TABLE jira_link DROP COLUMN IF EXISTS alarm_type")
    op.execute("DROP TABLE IF EXISTS integration_inbox")
