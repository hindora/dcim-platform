"""A credential says which key sealed it.

docs/26 Phase 4. Until now DCIM_CREDENTIAL_KEY has been one key for the
whole platform's life - rotating it, in the sense of "change the value and
restart", makes every already-stored secret_enc undecryptable, since
`decrypt_secret` has never had anywhere to learn which key a given blob was
sealed under. There was no way to rotate the key at all, only to lose every
device credential at once.

`key_id` is nullable and NULL for every row this migration touches - the
existing single-key deployment's behaviour is completely unchanged:
`decrypt_secret`/`encrypt_secret` treat a NULL key_id exactly as they always
have, resolving to DCIM_CREDENTIAL_KEY. A rotation (backend/scripts/
rotate_credential_key.py) decrypts a row under whatever key its key_id
names - DCIM_CREDENTIAL_KEY for NULL, DCIM_CREDENTIAL_KEY_RING[key_id]
otherwise - re-encrypts it under the new key, and writes the new key_id
alongside the new secret_enc in the same UPDATE, so a crash mid-rotation
leaves each row internally consistent and the script resumable: a row whose
key_id already matches the target is simply skipped on the next run.

This is scoped to the `credential` table only - certificate_authority.
key_enc and every other encrypt_secret/decrypt_secret caller (Jira
integration tokens, discovery's promotion blobs) are deliberately untouched.
Rotating THOSE is a real gap this migration does not close; widening key_id
rotation to every encrypted column is future work, not something this phase
needed to unblock device-credential rotation specifically.

Revision ID: 0087
Revises: 0086
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("credential", sa.Column("key_id", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("credential", "key_id")
