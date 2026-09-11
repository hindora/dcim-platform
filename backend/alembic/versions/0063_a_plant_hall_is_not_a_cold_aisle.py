"""Room-air rules that know which kind of room they are looking at.

Revision ID: 0063
Revises: 0062

The moment a transmitter was fitted in the chiller plant it raised
ambient_temp_high at 27.2 C, against a threshold of 27.0. That threshold is
ASHRAE TC 9.9's recommended maximum for RACK INTAKE AIR, and it is the right
number for a cold aisle. A chiller hall at 27 C is a chiller hall on a normal
day: pumps, compressors and a room whose entire job is to reject heat, usually
on ventilation rather than cooling.

An alarm that is always on is an alarm nobody reads, and it is worse than no
alarm at all, because it teaches an operator to skip the row.

The engine scopes a rule by device_type and metric instance, and every one of
these devices is a "sensor" - a rack probe and a room transmitter alike. So the
room transmitters publish their temperature under the instance ROOM, and this
migration splits the rules along it:

  instance ''      a rack probe in a cold aisle.  27 C warn / 38 C critical,
                   which is ASHRAE recommended and allowable.
  instance 'ROOM'  a room with no racks in it.    35 C warn / 40 C critical.

35 C is not a comfortable plant room, it is one that has lost its ventilation
or its outside air is extreme; 40 C is where switchgear derates and a battery
room is doing real damage to its cells.

One threshold pair for all four kinds of room is a compromise the engine
forces: a battery hall would ideally alarm tighter than a chiller hall, because
VRLA life halves per ~10 K above 25 C. That is per-ROOM configuration, which
this platform does not have yet. It is less of a gap than it sounds, because
the number that actually matters in a battery room is the battery's own
temperature, and that is reported and judged on the machine itself.

Humidity is deliberately left alone. 20-70 % is the right band for any room
with electronics in it, so those rules should reach a switchroom unchanged.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None

#: The cold-aisle rules, scoped to the probes that measure a cold aisle.
SCOPE = sa.text("""
    UPDATE alarm_rule SET instances = ARRAY['']::text[]
     WHERE name IN ('ambient-temp-high', 'ambient-temp-critical')
""")

INSERT = sa.text("""
    INSERT INTO alarm_rule (name, alarm_type, enabled, device_types, metric_key,
                            operator, threshold, clear_threshold, dwell_samples,
                            clear_dwell_samples, severity, message_tpl,
                            category, detection, metric_kind, raise_on, instances)
    -- raise_on is a BOOLEAN (the value of a boolean metric that raises), not a
    -- direction. A numeric rule carries the direction in `operator` and sets
    -- this true, which is what every existing threshold rule does.
    VALUES (:name, :alarm_type, true, ARRAY['sensor']::text[], 'ambient_temperature',
            '>', :threshold, :clear_threshold, :dwell, 2, CAST(:severity AS severity_t),
            :message, 'environmental', 'threshold', 'numeric', true,
            ARRAY['ROOM']::text[])
    ON CONFLICT (name) DO UPDATE
      SET threshold = EXCLUDED.threshold,
          clear_threshold = EXCLUDED.clear_threshold,
          instances = EXCLUDED.instances,
          enabled = true
""")


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(SCOPE)
    conn.execute(INSERT, [
        {"name": "facility-room-temp-high",
         "alarm_type": "ambient_temp_high",
         "threshold": 35, "clear_threshold": 33, "dwell": 3,
         "severity": "WARNING",
         "message": "Room air {value} C above {threshold} C - a room with no "
                    "racks in it, so this is its own ventilation or cooling, "
                    "not the hall's"},
        {"name": "facility-room-temp-critical",
         "alarm_type": "ambient_temp_critical",
         "threshold": 40, "clear_threshold": 37, "dwell": 2,
         "severity": "CRITICAL",
         "message": "Room air {value} C above {threshold} C - switchgear "
                    "derates and battery life burns at this temperature"},
    ])


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("""
        DELETE FROM alarm_rule
         WHERE name IN ('facility-room-temp-high', 'facility-room-temp-critical')
    """))
    # Back to matching every instance, which is what they did before.
    conn.execute(sa.text("""
        UPDATE alarm_rule SET instances = ARRAY[]::text[]
         WHERE name IN ('ambient-temp-high', 'ambient-temp-critical')
    """))
