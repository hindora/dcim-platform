"""An outlet's phase is not always one character.

`outlet.phase` was varchar(1), which fits the "A / B / C" of a single-phase
strip and nothing else. A three-phase rack PDU feeds each bank line-to-line, so
its outlets sit on `L1-L2`, `L2-L3`, `L3-L1` - five characters, and the value an
operator needs in order to know which pair a load actually draws from. The
column would have truncated or rejected every one of them.

It never got the chance, because the importer skipped outlets entirely (it read
`number` where the export says `index`), so nothing was ever inserted and the
narrow column was never exercised. Fixing the importer makes this column live
for the first time, and on this estate the very first rows are AP8886 outlets on
`L1-L2`.

Widened rather than normalised into a phase table on purpose: the value is a
label the vendor prints on the strip and reports in its MIB, not a foreign key,
and a three-character-wide domain does not earn a table.
"""

from alembic import op
import sqlalchemy as sa

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("outlet", "phase",
                    existing_type=sa.String(length=1),
                    type_=sa.String(length=8),
                    existing_nullable=True)


def downgrade() -> None:
    # Anything that does not fit a single character is a three-phase pairing,
    # and there is no shorter true form of it - so the down path drops the
    # value rather than inventing one.
    op.execute("UPDATE outlet SET phase = NULL WHERE length(phase) > 1")
    op.alter_column("outlet", "phase",
                    existing_type=sa.String(length=8),
                    type_=sa.String(length=1),
                    existing_nullable=True)
