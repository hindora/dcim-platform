"""Sites have a position on the globe.

The world map needs a latitude and longitude per site. Like every other piece
of geometry this is asset data - entered by an administrator or taken from the
site's address - never telemetry, and never geocoded over the network.

`location_source` says how good the figure is: 'manual' for a position someone
set, 'city centroid' for one backfilled from app/core/geo.py because only the
city was known. The map marks the second kind as approximate.

Existing sites are backfilled here from the same metro table (copied, not
imported: a migration must not change meaning when app code later does).

Revision ID: 0100
Revises: 0099
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None

# The metros the shipped estate uses, as of this migration.
_BACKFILL = {"chicago": (41.88, -87.63), "new york": (40.71, -74.01)}


def upgrade() -> None:
    op.add_column("datacenter", sa.Column("latitude", sa.Numeric(9, 6), nullable=True))
    op.add_column("datacenter", sa.Column("longitude", sa.Numeric(9, 6), nullable=True))
    op.add_column("datacenter", sa.Column("location_source", sa.Text(), nullable=True))
    op.create_check_constraint(
        "datacenter_lat_range", "datacenter",
        "latitude IS NULL OR latitude BETWEEN -90 AND 90")
    op.create_check_constraint(
        "datacenter_lon_range", "datacenter",
        "longitude IS NULL OR longitude BETWEEN -180 AND 180")
    for city, (lat, lon) in _BACKFILL.items():
        op.execute(sa.text("""
            UPDATE datacenter
               SET latitude = :lat, longitude = :lon, location_source = 'city centroid'
             WHERE latitude IS NULL AND lower(trim(city)) = :city
        """).bindparams(lat=lat, lon=lon, city=city))


def downgrade() -> None:
    # The metadata naming convention prefixes check constraints
    # (ck_<table>_<name>), so these are the names upgrade() actually created.
    op.drop_constraint("ck_datacenter_datacenter_lon_range", "datacenter", type_="check")
    op.drop_constraint("ck_datacenter_datacenter_lat_range", "datacenter", type_="check")
    op.drop_column("datacenter", "location_source")
    op.drop_column("datacenter", "longitude")
    op.drop_column("datacenter", "latitude")
