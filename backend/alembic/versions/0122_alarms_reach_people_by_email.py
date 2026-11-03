"""Alarms reach people by email, routed by severity and kind.

Every commercial DCIM has this beside ticketing, and for a different job: a
ticket queues WORK, an email (or the SMS gateway behind one) wakes the person on
call. Three parts, the way those products split them:

* `smtp_setting` - the one mail relay the site sends through. One row, pinned to
  id 1. The password is stored encrypted (`encrypt_secret`, the same envelope a
  Jira token uses) with a non-reversible hint beside it for the form.
* `notification_rule` - who gets what. Each rule has its own recipients and its
  own policy, the same clauses a ticketing integration uses (response class,
  minimum severity, categories, root causes only, nothing under maintenance,
  a delay before the first email), so "NOC on-call: critical and major
  alarms" and "Facilities: every power and cooling alert" are two rows.
* `notification_outbox` - one row per email to send, written in the SAME
  transaction as the alarm action that caused it, delivered by the ingest
  worker with retries. `notification_open` remembers which conditions a rule
  has already emailed about, so the clear (and any escalation) follows the
  first email whatever the policy has been edited to say since.

Revision ID: 0122
Revises: 0121
"""

from __future__ import annotations

from alembic import op

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS smtp_setting (
            id              smallint PRIMARY KEY DEFAULT 1,
            enabled         boolean NOT NULL DEFAULT false,
            host            text NOT NULL DEFAULT '',
            port            integer NOT NULL DEFAULT 587,
            security        text NOT NULL DEFAULT 'starttls',
            username        text NOT NULL DEFAULT '',
            password_secret bytea,
            password_hint   text,
            from_address    text NOT NULL DEFAULT '',
            from_name       text NOT NULL DEFAULT '',
            reply_to        text NOT NULL DEFAULT '',
            link_base_url   text NOT NULL DEFAULT '',
            updated_at      timestamptz NOT NULL DEFAULT now(),
            updated_by      text,
            CONSTRAINT smtp_setting_single_row CHECK (id = 1),
            CONSTRAINT smtp_setting_security CHECK (security IN ('starttls', 'tls', 'none')),
            CONSTRAINT smtp_setting_port CHECK (port BETWEEN 1 AND 65535)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS notification_rule (
            id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name                 text NOT NULL,
            enabled              boolean NOT NULL DEFAULT true,
            recipients           text[] NOT NULL,
            policy               jsonb NOT NULL DEFAULT '{}'::jsonb,
            notify_on_clear      boolean NOT NULL DEFAULT true,
            notify_on_escalation boolean NOT NULL DEFAULT true,
            max_per_hour         integer NOT NULL DEFAULT 30,
            created_at           timestamptz NOT NULL DEFAULT now(),
            updated_at           timestamptz NOT NULL DEFAULT now(),
            updated_by           text,
            CONSTRAINT notification_rule_name_unique UNIQUE (name),
            CONSTRAINT notification_rule_recipients CHECK (
                cardinality(recipients) BETWEEN 1 AND 20),
            CONSTRAINT notification_rule_max_per_hour CHECK (
                max_per_hour BETWEEN 1 AND 500)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS notification_outbox (
            id              bigserial PRIMARY KEY,
            rule_id         uuid NOT NULL REFERENCES notification_rule(id) ON DELETE CASCADE,
            kind            text NOT NULL,
            fingerprint     text NOT NULL,
            alarm_id        uuid,
            payload         jsonb NOT NULL,
            status          text NOT NULL DEFAULT 'pending',
            attempts        integer NOT NULL DEFAULT 0,
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            claimed_by      text,
            claimed_at      timestamptz,
            subject         text,
            message_id      text,
            note            text,
            created_at      timestamptz NOT NULL DEFAULT now(),
            sent_at         timestamptz,
            CONSTRAINT notification_outbox_kind CHECK (
                kind IN ('alarm_raised', 'alarm_escalated', 'alarm_cleared')),
            CONSTRAINT notification_outbox_status CHECK (
                status IN ('pending', 'sending', 'sent', 'skipped', 'suppressed', 'failed'))
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS notification_outbox_due
            ON notification_outbox (next_attempt_at, id) WHERE status = 'pending'
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS notification_outbox_condition
            ON notification_outbox (rule_id, fingerprint, id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS notification_outbox_recent
            ON notification_outbox (created_at DESC)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS notification_open (
            rule_id     uuid NOT NULL REFERENCES notification_rule(id) ON DELETE CASCADE,
            fingerprint text NOT NULL,
            alarm_id    uuid,
            opened_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (rule_id, fingerprint)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS notification_open")
    op.execute("DROP TABLE IF EXISTS notification_outbox")
    op.execute("DROP TABLE IF EXISTS notification_rule")
    op.execute("DROP TABLE IF EXISTS smtp_setting")
