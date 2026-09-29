"""A collector proves itself with a certificate, not a shared secret alone.

docs/26 Phase 2. The bearer token minted in migration 0085 is a fleet-wide
derivation: one master secret proves every collector, and a leak of the
master compromises the lot at once. A client certificate is per-collector,
short-lived and independently revocable, and unlike a bearer token it cannot
be replayed from a copy of a log line - the private key never leaves the
collector's disk.

This does NOT remove the bearer token. A hard cutover on a live fleet is
exactly the flag day the design notes on the earlier migrations argue against
(see 0085's "outdated collector keeps sending data" reasoning, borrowed from
Zabbix) - a collector that has not yet enrolled must keep working while it
transitions, or an upgrade window becomes an outage window. mTLS becomes the
preferred path; the token becomes what an unenrolled or pre-Phase-2 collector
falls back to, logged as such the same way an unscoped token already is.

**The CA.** `certificate_authority` holds intermediate CA certificates this
platform trusts to sign - not the root. The root's private key is generated
once, to a file an operator is told to move offline (see
`backend/app/services/ca.py`'s bootstrap tool), and this running process
never loads it again: every leaf cert this platform issues is signed by the
intermediate, whose key IS kept here (`key_enc`, sealed the same way device
credentials are - `DCIM_CREDENTIAL_KEY`, AES-256-GCM) because an intermediate
has to be online to sign enrollments on demand. `is_active` lets a new
intermediate roll in without touching certificates the old one already
issued: they keep verifying against the same root either way, since clients
trust the root, not any one intermediate.

**Enrollment.** `enrollment_token` is single-use and short-lived (the service
default is 24h) and bound to one collector row before it exists as a
connection - the pre-created record IS the approval, the same choice the
sharding design in 0085 already made for `state = 'pending'`. Only the hash
is stored, the same choice `collector_token`'s HMAC derivation makes for the
same reason: the row is not the secret.

**Per-collector fields.** `cert_serial` is what a per-request check compares
the presented certificate against - revocation without a CRL, which the
docs/26 write-up chose deliberately because CRL distribution to a remote or
air-gapped site is fragile. Overwriting it on renewal is how the old
certificate stops being current without a separate revoke step.
`encryption_pubkey` is collected at enrollment because that is when the
collector generates its keypair, but nothing reads it yet - it is inert until
Phase 4 seals credentials to it.

Revision ID: 0086
Revises: 0085
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "certificate_authority",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("serial", sa.Text, nullable=False, unique=True),
        sa.Column("cert_pem", sa.Text, nullable=False),
        # NULL for 'root': this running process never holds that key.
        sa.Column("key_enc", sa.LargeBinary),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False,
                  server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("kind IN ('root', 'intermediate')",
                           name="certificate_authority_kind"),
    )
    # At most one active intermediate at a time - the one new enrollments and
    # renewals are signed with. A rotation inserts the new one active and
    # flips the old one inactive in the same transaction; it keeps verifying
    # what it already issued regardless, since that check is against the
    # root, not against `is_active`.
    op.create_index(
        "ix_certificate_authority_one_active_intermediate",
        "certificate_authority", ["kind"], unique=True,
        postgresql_where=sa.text("kind = 'intermediate' AND is_active"))

    op.create_table(
        "enrollment_token",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("collector_id", sa.Text,
                  sa.ForeignKey("collector_instance.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_enrollment_token_collector", "enrollment_token",
                    ["collector_id"])

    op.add_column("collector_instance", sa.Column(
        "cert_serial", sa.Text))
    op.add_column("collector_instance", sa.Column(
        "cert_fingerprint_sha256", sa.Text))
    op.add_column("collector_instance", sa.Column(
        "cert_not_after", sa.DateTime(timezone=True)))
    # An admin's explicit "stop trusting this one" - distinct from
    # decommissioning the whole collector, for a suspected key compromise
    # where the collector itself should re-enroll and keep its history.
    op.add_column("collector_instance", sa.Column(
        "cert_revoked_at", sa.DateTime(timezone=True)))
    op.add_column("collector_instance", sa.Column(
        "encryption_pubkey", sa.Text))
    op.add_column("collector_instance", sa.Column(
        "enrolled_by", sa.Text))
    op.add_column("collector_instance", sa.Column(
        "enrolled_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    for column in ("enrolled_at", "enrolled_by", "encryption_pubkey",
                   "cert_revoked_at", "cert_not_after",
                   "cert_fingerprint_sha256", "cert_serial"):
        op.drop_column("collector_instance", column)
    op.drop_index("ix_enrollment_token_collector", table_name="enrollment_token")
    op.drop_table("enrollment_token")
    op.drop_index("ix_certificate_authority_one_active_intermediate",
                  table_name="certificate_authority")
    op.drop_table("certificate_authority")
