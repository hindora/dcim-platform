"""Audit log: who changed what, and who was handed secrets.

Two kinds of entry live here and they answer different questions.

Writes - inventory edits, alarm acknowledgements, rule changes, discovery
promotions - answer "who did this and what did it look like before". Those carry
``before`` and ``after``.

Credential reads answer a different question: the assignments endpoint is the
one place in the system that hands out decrypted device credentials, and the
only useful record of a compromise is a log of every time that happened, with
the identity and the address it went to. That entry has no before or after; the
fact of the fetch is the whole content.

``ip`` is inet rather than text so that a range query works when someone asks
which of the management subnets pulled credentials last week.
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE audit_log (
            id          bigserial PRIMARY KEY,
            ts          timestamptz NOT NULL DEFAULT now(),
            -- 'admin', 'collector:col-1', 'system'. Free text because the
            -- actor is not always a row in a table we own.
            actor       text NOT NULL,
            action      text NOT NULL,
            target_type text,
            target_id   text,
            before      jsonb,
            after       jsonb,
            ip          inet,
            user_agent  text,
            -- Deliberately not a foreign key to anything: an audit row must
            -- outlive the thing it describes. A device deleted next year must
            -- not take its own deletion record with it.
            outcome     text NOT NULL DEFAULT 'ok'
        )
    """)
    # The two queries this table actually gets asked: "what happened recently"
    # and "what did this actor/target do".
    op.execute("CREATE INDEX ix_audit_ts ON audit_log (ts DESC)")
    op.execute("CREATE INDEX ix_audit_actor ON audit_log (actor, ts DESC)")
    op.execute("CREATE INDEX ix_audit_target ON audit_log (target_type, target_id, ts DESC)")
    # Credential handouts and failures are the rows an investigation starts
    # from, and they are a small minority of the table.
    op.execute("""
        CREATE INDEX ix_audit_sensitive ON audit_log (ts DESC)
         WHERE action LIKE 'credential.%' OR outcome <> 'ok'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_log")
