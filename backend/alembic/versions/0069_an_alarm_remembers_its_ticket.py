"""fingerprint -> issue key, stored on our side.

Revision ID: 0069
Revises: 0068

THE STEP EVERY INTEGRATION SKIPS. LibreNMS's Jira transport does not store the
issue key, so every firing of the same alert creates a new issue. Zabbix does
store it - written back onto the event as a tag - and that single difference is
why one of the two is usable. This table is that tag.

WITHOUT IT the dispatcher would have to ask Jira "is there already an issue for
this?" on every single action. That is a JQL search per alarm against an API
with an hourly point quota and a 100-RPS burst limit, on the one code path that
runs during an alarm storm - precisely when the quota is least affordable and
the answer is most needed. Searching stays, but as a NIGHTLY RECONCILER looking
for drift, not as a lookup.

THE KEY IS THE FINGERPRINT, NOT THE ALARM ID. An alarm row is cleared and a new
row is raised for the same condition an hour later with a new uuid; the ticket
is about the condition, not the row. `alarm_id` is carried alongside for the
console link and is deliberately allowed to go stale.

`closed_at` IS OURS, NOT JIRA'S. It records when we last saw the issue leave
the open state, which is what the reopen window is measured from. Jira's own
resolution date is in `resolution`/`status` and may disagree after a manual
reopen - and when they disagree, the reconciler corrects us.
"""

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE jira_link (
            fingerprint     text PRIMARY KEY,
            integration_id  uuid NOT NULL
                            REFERENCES integration(id) ON DELETE CASCADE,
            issue_key       text NOT NULL,
            issue_id        text,

            -- The alarm this was last pushed for. SET NULL rather than CASCADE:
            -- losing the alarm must not lose the fact that a ticket exists,
            -- or the next raise opens a duplicate.
            alarm_id        uuid,
            device_id       uuid REFERENCES device(id) ON DELETE SET NULL,

            -- What Jira last told us, from the webhook or the reconciler.
            -- `status_category` is the one worth branching on: status NAMES
            -- are per-workflow and a customer who renamed Done to Resolved
            -- would break any comparison against the name.
            status          text,
            status_category text,
            resolution      text,

            opened_at       timestamptz NOT NULL DEFAULT now(),
            closed_at       timestamptz,
            -- Set when the issue was resolved as Won't Fix or Duplicate. The
            -- reopen path checks it: re-opening something a human explicitly
            -- declined is how an integration loses its welcome.
            wont_reopen     boolean NOT NULL DEFAULT false,

            last_pushed_at  timestamptz,
            push_count      integer NOT NULL DEFAULT 0,

            -- PROJ-123. Validated here because a malformed key would be
            -- written into a URL and a JQL string, and both would fail in
            -- ways that read as a Jira outage.
            CONSTRAINT ck_jira_link_key
                CHECK (issue_key ~ '^[A-Z][A-Z0-9_]+-[0-9]+$')
        )
    """)

    # The inbound direction. A webhook arrives at Atlassian's cadence carrying
    # a key and nothing else, so this lookup is on the critical path of a
    # request that must answer inside 30 seconds.
    op.execute("""
        CREATE INDEX ix_jira_link_issue
            ON jira_link (integration_id, issue_key)
    """)
    # "Is there an open ticket for this condition" - the dispatcher's question.
    op.execute("""
        CREATE INDEX ix_jira_link_open
            ON jira_link (fingerprint) WHERE closed_at IS NULL
    """)
    # The console's question, per device.
    op.execute("""
        CREATE INDEX ix_jira_link_device
            ON jira_link (device_id) WHERE device_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS jira_link")
