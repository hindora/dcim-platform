"""Seven categories: nothing is filed as uncategorised any more.

The eighth bucket was the instrument that measured the taxonomy - a condition
nobody classified had to be countable rather than filed into whichever category
was nearest. It has now read zero for its whole life: 29,545 historical alarms
and every trap, point and alarm type the plane can emit resolve to a real
category. A column that is always empty stops being an instrument and becomes
furniture, and an operator cannot route "uncategorised" anyway.

The gap it used to detect is now caught earlier and harder: `test_alert_taxonomy`
fails if any condition the plane can emit resolves through the fallback, so a
point added upstream and forgotten breaks the build instead of appearing in a
column nobody watches.

Anything stamped `uncategorised` before this is re-stamped `visibility` - the
fallback the classifier now uses, on the grounds that a condition we cannot
type, cannot match to a metric and cannot attach to a device is a statement
about our own understanding rather than about equipment.

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.core.alert_taxonomy import FALLBACK

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

_OLD = "uncategorised"


def upgrade() -> None:
    conn = op.get_bind()
    moved = conn.execute(sa.text("""
        UPDATE alarm SET category = :fallback WHERE category = :old
    """), {"fallback": FALLBACK, "old": _OLD}).rowcount
    # A rule may carry an override, and an override naming a category that no
    # longer exists would classify every alarm it raises into nothing.
    conn.execute(sa.text("""
        UPDATE alarm_rule SET category = NULL WHERE category = :old
    """), {"old": _OLD})
    print(f"0024: re-stamped {moved} alarms from {_OLD} to {FALLBACK}")


def downgrade() -> None:
    # Deliberately not reversible. The rows that were `uncategorised` are
    # indistinguishable afterwards from the ones that were always `visibility`,
    # and inventing a rule to split them again would be worse than leaving them.
    pass
