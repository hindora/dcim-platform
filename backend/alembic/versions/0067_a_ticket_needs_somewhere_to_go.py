"""Where tickets go, and the credential that gets them there.

Revision ID: 0067
Revises: 0066

One row per configured Atlassian instance. The shape is `collector_config`'s
deliberately: a sparse JSONB document plus an integer version, because the
same two properties are wanted here. Sparse, so a default that changes in a
release reaches every install that never overrode it. Versioned with an
integer rather than a timestamp, so "what is stored" and "what is in force"
stay two answerable questions.

TWO SECRETS, TWO COLUMNS. The API credential and the webhook secret are
different credentials with different blast radii - one can open tickets on the
customer's Jira, the other only proves an inbound POST came from it - and
rotating one must not disturb the other. Both are AES-256-GCM under the same
DCIM_CREDENTIAL_KEY as device credentials (app/core/security.py), and neither
ever leaves the process: `secret_hint` is the only part any API returns.

WHY `secret_expires_at` IS A COLUMN AND NOT A CONFIG KEY. Atlassian Cloud API
tokens created after December 2024 expire within a year, and the whole
pre-December-2024 generation was force-expired in spring 2026. A silently
expired token means this platform stops opening tickets and nothing says so -
which is the same failure class as a dead collector, and it gets the same
treatment: a column the platform monitor can read, and a platform alarm at 30
and 7 days. Burying it in `config` would make that query a JSONB scan and, more
to the point, would make it look optional.
"""

from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE integration (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            -- 'jira_cloud' | 'jira_dc' | 'jsm_ops'. The auth mode and the base
            -- URL shape both follow from it, so it is not derivable from the
            -- URL and must be declared.
            kind        text NOT NULL,
            name        text NOT NULL UNIQUE,
            -- Off until someone has run the connection test. A half-configured
            -- integration that starts posting on save is how a customer's
            -- service desk gets its first hundred malformed tickets.
            enabled     boolean NOT NULL DEFAULT false,
            base_url    text NOT NULL,
            -- Only the Operations alert API needs it, and only on Cloud.
            cloud_id    text,
            -- Sparse: project key, issue/request type, the policy, the
            -- severity map, resolved field ids. Absent keys fall through to
            -- the defaults in app/integrations/config.py.
            config      jsonb NOT NULL DEFAULT '{}'::jsonb,
            version     integer NOT NULL DEFAULT 1,

            -- nonce || ciphertext || tag, AES-256-GCM. Never returned.
            secret_enc  bytea NOT NULL,
            secret_hint text,
            -- 'api_token' (Cloud Basic) | 'pat' (Data Center Bearer).
            secret_kind text NOT NULL,
            secret_expires_at timestamptz,

            -- The inbound half. NULL until a webhook is registered; the
            -- webhook endpoint refuses every request while it is NULL rather
            -- than accepting unsigned ones.
            webhook_secret_enc bytea,
            -- A high-entropy path segment, so an unsigned probe does not even
            -- reach the HMAC check. Defence in depth, not the defence.
            webhook_token text,
            webhook_id    text,
            webhook_expires_at timestamptz,

            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            updated_by  text,

            CONSTRAINT ck_integration_kind
                CHECK (kind IN ('jira_cloud','jira_dc','jsm_ops')),
            CONSTRAINT ck_integration_secret_kind
                CHECK (secret_kind IN ('api_token','pat','oauth2'))
        )
    """)

    # At most one ENABLED integration per kind.
    #
    # Not a limit on how many can be configured - a staging Jira alongside a
    # production one is reasonable and both rows exist - but two enabled
    # instances of the same kind would each open a ticket for every alarm, and
    # the dedup that prevents duplicates is keyed per integration and cannot
    # see across them. Whoever wants a second one turns the first one off.
    op.execute("""
        CREATE UNIQUE INDEX ix_integration_enabled_kind
            ON integration (kind) WHERE enabled
    """)
    # The webhook endpoint's only lookup: token -> integration, before any
    # body is parsed.
    op.execute("""
        CREATE UNIQUE INDEX ix_integration_webhook_token
            ON integration (webhook_token) WHERE webhook_token IS NOT NULL
    """)
    # What the platform monitor sweeps: anything enabled with an expiry in
    # sight. Partial so the scan stays proportional to what is configured
    # rather than to what was ever configured.
    op.execute("""
        CREATE INDEX ix_integration_expiry
            ON integration (secret_expires_at)
         WHERE enabled AND secret_expires_at IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS integration")
