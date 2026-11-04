"""A hot reading is not an impossible one: DQ-3's air bounds widened.

DQ-3 exists to catch a reading no real condition can produce - a failed
thermistor, a -127 or 85.0 sentinel - and it MASKS what it catches, so the
reading stops counting as evidence. Its air bounds were set at the edge of a
healthy room, which a cooling failure crosses on its own:

- hall-crahs, 2026-11-04: seven stopped CRAHs discharged at 40.7-40.8 C, which
  is what a stopped unit does - its discharge soaks toward the return air -
  and DQ-3 called the readings "physically impossible" and dropped them. The
  plainest evidence of the failure was masked by the check meant to protect
  the evidence.
- Rack inlets reached 36.8 C in three minutes of that test; a longer total
  loss passes the old 50 C inlet and ambient bounds (cascade reports put
  areas above 40 C within 15 minutes, docs/28).

The new bounds sit beyond anything an air path can reach with the plant off:
supply 65 C, return / inlet / ambient 70 C. Water and humidity are unchanged.
The lower bounds are unchanged: a sub-zero air reading is still a sensor.

Revision ID: 0130
Revises: 0129
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "0130"
down_revision = "0129"
branch_labels = None
depends_on = None

_OLD_BOUNDS = {"supply_air_temp": [5, 40], "return_air_temp": [10, 55],
               "inlet_temperature": [5, 50], "ambient_temperature": [0, 50],
               "water_supply_temp": [1, 60], "relative_humidity": [0.5, 99.5]}

_NEW_BOUNDS = {"supply_air_temp": [5, 65], "return_air_temp": [10, 70],
               "inlet_temperature": [5, 70], "ambient_temperature": [0, 70],
               "water_supply_temp": [1, 60], "relative_humidity": [0.5, 99.5]}

#: Read by tests/anomaly_fakes.seed_rows like 0113's and 0129's tuning.
#: `params || update` replaces the whole bounds object, so all of it is listed.
PARAM_UPDATES = {"DQ-3": {"bounds": _NEW_BOUNDS}}


def _set(bounds: dict, who: str) -> None:
    op.get_bind().execute(sa.text("""
        UPDATE anomaly_detector SET params = params || CAST(:p AS jsonb),
               updated_at = now(), updated_by = :who
         WHERE id = 'DQ-3'
    """), {"p": json.dumps({"bounds": bounds}), "who": who})


def upgrade() -> None:
    _set(_NEW_BOUNDS, "migration:0130")


def downgrade() -> None:
    _set(_OLD_BOUNDS, "migration:0130-down")
