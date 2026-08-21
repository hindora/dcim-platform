"""secret_hint stopped containing the secret.

A credential hint exists so a human can tell two credentials apart. The obvious
form - ``community: <value>`` - is the entire credential written out, and it is
the one credential column that is NOT encrypted and IS returned by
``GET /devices/{id}/endpoints`` to any authenticated reader.

It looked harmless because this fleet's SNMP community is the device's own IP
address, which is not a secret. Against real hardware the same code publishes
the real community string to every viewer account.

The rewrite is a pure text transform - ``community: 10.51.13.27`` becomes
``community (11 chars)`` - so it needs no decryption key and can run anywhere.
Length is kept because it is genuinely useful for telling credentials apart and
is not the secret.

Redfish hints of the form ``user: admin`` are left alone: a username is not the
credential, and stripping it would remove the only thing that makes the hint
worth having.
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

# The leaking shapes: a label, a colon, then the value itself.
_LEAKY = ("community", "password", "passphrase", "token", "secret",
          "private_key", "auth_key", "priv_key")


def upgrade() -> None:
    for label in _LEAKY:
        op.execute(f"""
            UPDATE credential
               SET secret_hint = '{label} (' ||
                     length(trim(substring(secret_hint from '{label}:\\s*(.*)$')))
                     || ' chars)'
             WHERE secret_hint ~ '^{label}:\\s*.+'
        """)


def downgrade() -> None:
    """Deliberately not reversible.

    The original hints contained the secrets, and this migration is the only
    reason they no longer do. Restoring them would mean re-deriving the values
    from the encrypted column and writing them back out in plaintext, which is
    the vulnerability, performed on purpose. Re-import regenerates hints in the
    safe form.
    """
