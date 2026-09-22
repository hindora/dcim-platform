"""The outbox: an intent to ticket, committed with the alarm that caused it.

Revision ID: 0068
Revises: 0067

WHY NOT THE FAN-OUT. Every alarm lifecycle action already reaches a publisher -
`app/ingest/fanout.py` - and reusing it for ticketing would have been one line.
It is the wrong transport, for a reason written into that file: the fan-out
swallows its own errors on purpose, because a WebSocket frame that fails to
publish must never break ingestion. A browser that misses a frame refreshes. A
ticket that is never opened is an outage nobody was told about.

WHY NOT A REDIS STREAM. The same objection plus a worse one: a stream entry is
not written in the transaction that raised the alarm, so a crash between the
COMMIT and the XADD loses the intent with no trace. And Redis on this
deployment is the memory-fragile piece - it has been OOM-killed twice, and a
ticket backlog is exactly the kind of unbounded queue that would do it again.

SO: a table, written by the same session that writes the alarm. The alarm and
the intent to ticket it commit together or not at all.

WHY `payload` IS FROZEN. The alarm row mutates under us - severity escalates,
`occurrence_count` climbs, the message is rewritten by the next detector to
speak. A comment that says "this was MAJOR at 09:14" must still say that when
it is finally delivered after an hour of 429s. The row records what was true
when the action happened, and the dispatcher never re-reads the alarm.

WHY THERE IS NO FOREIGN KEY ON `alarm_id`. A dead letter is kept for a human
to look at, and the alarm behind it may be purged first. ON DELETE SET NULL
would work; the reason it is not used is that we want the id even when the
alarm is gone - it is in the payload anyway and the column is what makes the
join cheap while the alarm exists.
"""

from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE integration_outbox (
            id             bigserial PRIMARY KEY,
            integration_id uuid NOT NULL
                           REFERENCES integration(id) ON DELETE CASCADE,
            -- alarm_raised | alarm_escalated | alarm_cleared | alarm_acked
            kind           text NOT NULL,
            -- The dedup key. sha256 over (site, device, alarm_type, instance),
            -- computed in app/integrations/fingerprint.py and deliberately
            -- excluding severity, value and every timestamp: a WARNING that
            -- escalates to CRITICAL is the SAME condition and must land on the
            -- same ticket.
            fingerprint    text NOT NULL,
            alarm_id       uuid,
            payload        jsonb NOT NULL,

            created_at     timestamptz NOT NULL DEFAULT now(),
            -- When this row may next be attempted. Carries both the backoff
            -- schedule and any Retry-After Atlassian handed back; a single
            -- column so the claim query is one indexed range scan whichever
            -- of the two is in force.
            not_before     timestamptz NOT NULL DEFAULT now(),
            attempts       smallint NOT NULL DEFAULT 0,

            claimed_by     text,
            claimed_at     timestamptz,

            state          text NOT NULL DEFAULT 'pending',
            last_error     text,

            CONSTRAINT ck_outbox_state
                CHECK (state IN ('pending','done','dead'))
        )
    """)

    # The claim query, and the only one on the hot path:
    #   WHERE state = 'pending' AND not_before <= now() ORDER BY id
    #   FOR UPDATE SKIP LOCKED
    #
    # Ordered by id inside the index so the scan is already in delivery order.
    # That ordering is load-bearing: a clear must never overtake its own raise,
    # and id is the only monotonic thing here (two rows can share a created_at
    # to the microsecond when one batch raises and clears in the same tick).
    op.execute("""
        CREATE INDEX ix_outbox_due ON integration_outbox (not_before, id)
         WHERE state = 'pending'
    """)
    # "What happened to this condition" - the settings page and the reconciler.
    op.execute("""
        CREATE INDEX ix_outbox_fingerprint
            ON integration_outbox (fingerprint, created_at DESC)
    """)
    # Dead letters are a small set that is read often and must never be found
    # by a sequential scan over a table that has delivered millions.
    op.execute("""
        CREATE INDEX ix_outbox_dead ON integration_outbox (integration_id, id DESC)
         WHERE state = 'dead'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS integration_outbox")
