"""An Operations alert is closed by its clear.

The ticketing outbox lets a clear through only for a condition that already has
something open - otherwise every clear on the estate would become a call to the
service desk about a condition it never heard of. For a Jira issue "open" is a
`jira_link` row, written when the issue is created. A JSM Operations alert keeps
no link (the alias is its key and the API holds it), so nothing ever counted as
open, every clear was dropped at the outbox, and every alert stayed open until
somebody closed it by hand - paging about faults that were long over.

`ops_alert_open` is that missing memory: one row per (integration, condition)
an alert was queued for, written in the same transaction as the raise and
removed with the clear. It records intent, not delivery - a raise that later
dies leaves a row whose clear finds no open alert, which the target already
treats as "nothing to close".

Backfill: conditions that were alerted (pending or delivered) and never
cleared, and whose alarm is still live, so their clear can close them.

Revision ID: 0123
Revises: 0122
"""

from __future__ import annotations

from alembic import op

revision = "0123"
down_revision = "0122"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ops_alert_open (
            integration_id uuid NOT NULL REFERENCES integration(id) ON DELETE CASCADE,
            fingerprint    text NOT NULL,
            alarm_id       uuid,
            opened_at      timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (integration_id, fingerprint)
        )
    """)
    op.execute("""
        INSERT INTO ops_alert_open (integration_id, fingerprint, alarm_id, opened_at)
        SELECT DISTINCT ON (o.integration_id, o.fingerprint)
               o.integration_id, o.fingerprint, o.alarm_id, o.created_at
          FROM integration_outbox o
          JOIN integration i ON i.id = o.integration_id AND i.kind = 'jsm_ops'
          LEFT JOIN alarm a ON a.id = o.alarm_id
         WHERE o.kind IN ('alarm_raised', 'alarm_escalated')
           AND o.state IN ('pending', 'done')
           AND (a.id IS NULL OR a.state <> 'CLEARED')
           AND NOT EXISTS (
               SELECT 1 FROM integration_outbox c
                WHERE c.integration_id = o.integration_id
                  AND c.fingerprint = o.fingerprint
                  AND c.kind = 'alarm_cleared' AND c.id > o.id)
         ORDER BY o.integration_id, o.fingerprint, o.id DESC
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ops_alert_open")
