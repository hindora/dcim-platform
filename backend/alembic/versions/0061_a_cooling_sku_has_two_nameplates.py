"""A cooling SKU has two nameplates, and the platform recorded one.

Revision ID: 0061
Revises: 0060

`model.rated_power_w` is what a machine DRAWS. For a CRAH that is about 6.5 kW,
and it says nothing about the 100 kW of heat the same unit is built to remove -
two different numbers about two different things. Without the second, the
cooling table could only report delivered cooling as a percentage of a rating
the platform did not hold: the model name said 100 kW and nothing behind it
did, so a hall's units could not be added up against the load they were
carrying.

Nullable, and on the MODEL rather than the device, because it is a datasheet
figure: every PCW 100kW removes 100 kW. NULL means the platform has no rating
for that SKU, which is the ordinary case for everything that is not cooling
gear, and the page shows a percentage there rather than inventing kilowatts.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model", sa.Column("rated_cooling_w", sa.Integer(),
                                     nullable=True))


def downgrade() -> None:
    op.drop_column("model", "rated_cooling_w")
