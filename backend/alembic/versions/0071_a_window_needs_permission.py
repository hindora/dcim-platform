"""A maintenance window, its change request, and the gate between them.

Revision ID: 0071
Revises: 0070

`change_ref` has been on `maintenance_window` since migration 0046 and has
never been read by anything. It stays exactly as it is - free text, whatever
somebody typed, possibly a ticket in a system this platform has never heard
of. What is added beside it is the half this platform OWNS: the issue it
opened itself, and whether that issue has been approved.

Two columns rather than reusing `change_ref` for both, because they are
different kinds of claim. One is a human saying "this is the change"; the
other is a key this platform created and can look up. When they disagree, the
UI shows both and says so, which is better than a single field that quietly
means different things depending on who filled it in.

THE GATE. `require_approval` makes the ticker refuse to start the window until
`jira_approval_state` is `approved`. That refusal is expressed in the ticker's
OWN query rather than as a check in the service, for the same reason `status`
is a column and not a comparison against now(): one process decides, and a
predicate in SQL cannot be forgotten by the next person to add a transition.

A held window is not silent. `starts_at` passing while approval is outstanding
is exactly the situation somebody needs telling about - the engineer is at the
door - so the service reports it and the API surfaces it on the window.

WHY THE OUTBOX LEARNS ABOUT WINDOWS. A change request is not a condition: it
has no device, no severity and no fingerprint, and `jira_link` is keyed by a
condition's identity. So a window's messages carry `window_id` and are written
back to `maintenance_window`, while an alarm's carry `alarm_id` and are
written back to `jira_link`. The CHECK keeps a row from claiming to be both.
"""

from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------ the window's own half
    op.execute("ALTER TABLE maintenance_window ADD COLUMN jira_issue_key text")
    op.execute("""
        ALTER TABLE maintenance_window ADD COLUMN jira_integration_id uuid
            REFERENCES integration(id) ON DELETE SET NULL
    """)
    # NULL when nothing is waiting on anybody - which is most windows, and is
    # NOT the same as "not yet approved". A window that never asked for
    # approval must not read as one that is still waiting for it.
    op.execute("ALTER TABLE maintenance_window ADD COLUMN jira_approval_state text")
    op.execute("""
        ALTER TABLE maintenance_window ADD COLUMN require_approval boolean
            NOT NULL DEFAULT false
    """)
    op.execute("""
        ALTER TABLE maintenance_window ADD CONSTRAINT ck_mw_approval_state
            CHECK (jira_approval_state IS NULL
                   OR jira_approval_state IN ('pending','approved','declined'))
    """)
    # The inbound lookup: a webhook arrives carrying an issue key and nothing
    # else, and it has to decide in one probe whether that key is a condition's
    # ticket or a window's change request.
    op.execute("""
        CREATE INDEX ix_mw_jira_issue ON maintenance_window (jira_issue_key)
         WHERE jira_issue_key IS NOT NULL
    """)
    # What the ticker skips over. Partial, so the gate costs nothing on an
    # estate where no window has ever asked for approval.
    op.execute("""
        CREATE INDEX ix_mw_awaiting_approval ON maintenance_window (starts_at)
         WHERE require_approval AND status = 'scheduled'
    """)

    # ------------------------------------------------ the outbox's own half
    op.execute("""
        ALTER TABLE integration_outbox ADD COLUMN window_id uuid
            REFERENCES maintenance_window(id) ON DELETE CASCADE
    """)
    # A message is about a condition or about a window, never both. Neither is
    # also legal: a platform alarm has no device and no window.
    op.execute("""
        ALTER TABLE integration_outbox ADD CONSTRAINT ck_outbox_one_subject
            CHECK (alarm_id IS NULL OR window_id IS NULL)
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE integration_outbox "
               "DROP CONSTRAINT IF EXISTS ck_outbox_one_subject")
    op.execute("ALTER TABLE integration_outbox DROP COLUMN IF EXISTS window_id")
    op.execute("DROP INDEX IF EXISTS ix_mw_awaiting_approval")
    op.execute("DROP INDEX IF EXISTS ix_mw_jira_issue")
    op.execute("ALTER TABLE maintenance_window "
               "DROP CONSTRAINT IF EXISTS ck_mw_approval_state")
    for column in ("require_approval", "jira_approval_state",
                   "jira_integration_id", "jira_issue_key"):
        op.execute(f"ALTER TABLE maintenance_window DROP COLUMN IF EXISTS {column}")
