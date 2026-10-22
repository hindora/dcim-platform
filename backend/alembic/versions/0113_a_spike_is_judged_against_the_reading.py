"""A spike is judged against the reading, and hunting means a real swing.

Tuning from the first live sweep after Phase 1 went in (docs/33 task 1.19):
66 findings and one alert on a clean estate, none of them a fault.

  DQ-4  min_dev_rel 0.02  The spike test's spread is never taken below 2 % of
                          the reading. A CRAH's power draw arrives in 10 W
                          steps round 430 W, its MAD is ~0, and every ordinary
                          step read as a spike: 46 false findings on power,
                          flow and pressure, which had no unit-family floor
                          at all (catalogue.UNIT_FAMILY now has one for each).
  VA-2, CR-6  min_step_pct 10
                          A reversal must move the valve 10 % to count. The
                          simulator's valves jitter 3-6 % between polls on a
                          steady loop; real hunting is a sustained swing of
                          10 % or more.

Merged into the stored params only where the key is absent, so an operator's
PATCH is never overwritten; the downgrade removes them again.

Revision ID: 0113
Revises: 0112
"""

from __future__ import annotations

import json

from sqlalchemy import text

from alembic import op

revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None

#: The tuning, as {detector_id: {param: value}}. tests/anomaly_fakes.py applies
#: it on top of 0112's seed rows, so a schema default and the effective seed
#: cannot drift apart.
PARAM_UPDATES: dict[str, dict] = {
    "DQ-4": {"min_dev_rel": 0.02},
    "VA-2": {"min_step_pct": 10},
    "CR-6": {"min_step_pct": 10},
}

#: The valve-hunting step (VA-2, and CR-6 on a CRAH's CHW valve) changes
#: meaning for an operator who already tuned it, so it is applied over the old
#: seed value only (5), never over a different PATCH.
_REPLACE_IF_SEED = {("VA-2", "min_step_pct"): 5, ("CR-6", "min_step_pct"): 5}


def upgrade() -> None:
    conn = op.get_bind()
    for det, updates in PARAM_UPDATES.items():
        for key, value in updates.items():
            old = _REPLACE_IF_SEED.get((det, key))
            conn.execute(text("""
                UPDATE anomaly_detector
                   SET params = params || jsonb_build_object(CAST(:k AS text),
                                                             CAST(:v AS jsonb))
                 WHERE id = :d
                   AND (NOT params ? :k
                        OR (CAST(:old AS jsonb) IS NOT NULL
                            AND params -> :k = CAST(:old AS jsonb)))
            """), {"d": det, "k": key, "v": json.dumps(value),
                   "old": json.dumps(old) if old is not None else None})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("UPDATE anomaly_detector SET params = params - 'min_dev_rel' "
                      "WHERE id = 'DQ-4'"))
    conn.execute(text("""UPDATE anomaly_detector
                            SET params = jsonb_set(params, '{min_step_pct}', '5')
                          WHERE id IN ('VA-2', 'CR-6')
                            AND params -> 'min_step_pct' = '10'"""))
