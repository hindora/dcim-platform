"""A switch's temperature is collected, and can raise something.

A campaign across every IT fault type found one that produced nothing at all:
an OOB switch ramped to 93 C in the simulator and the platform never noticed.
Neither channel covered it. No trap, because the device plane's temperature
rule excluded the type; and no poll, because the network poll profiles carry
`system` and `interfaces` and nothing that reads a sensor.

Both halves are fixed here for every network device type, not just the console
switches that exposed it: a router, a firewall and a load balancer were in the
same position, saved only by their vendor MIB.

  - `network_sensors` joins the network profiles. It reads ENTITY-SENSOR-MIB,
    which is the vendor-neutral place this gear reports how hot it is.

  - the cpu-temp rules stop being server-only. The thresholds stay where they
    are: 80/90 C is a die temperature, and an ASIC has the same physics as a
    CPU.

Revision ID: 0036
Revises: 0035
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


#: The profiles network gear actually runs, from the importer.
NETWORK_PROFILES = (
    "snmp-network-600s",
    "snmp-network-cisco-600s",
    "snmp-network-host-600s",
)

ADD_GROUP = sa.text("""
    UPDATE poll_profile
       SET metric_groups = array_append(metric_groups, 'network_sensors')
     WHERE name = ANY(:names)
       AND NOT ('network_sensors' = ANY(metric_groups))
""")

DROP_GROUP = sa.text("""
    UPDATE poll_profile
       SET metric_groups = array_remove(metric_groups, 'network_sensors')
     WHERE name = ANY(:names)
""")

# The die-temperature rules were written when only servers reported one.
WIDEN_RULES = sa.text("""
    UPDATE alarm_rule
       SET device_types = ARRAY['server', 'switch', 'router', 'firewall',
                                'load_balancer', 'oob_switch']
     WHERE alarm_type IN ('cpu_temp_high', 'cpu_temp_critical')
""")

NARROW_RULES = sa.text("""
    UPDATE alarm_rule
       SET device_types = ARRAY['server']
     WHERE alarm_type IN ('cpu_temp_high', 'cpu_temp_critical')
""")


def upgrade() -> None:
    bind = op.get_bind()
    added = bind.execute(ADD_GROUP, {"names": list(NETWORK_PROFILES)}).rowcount
    widened = bind.execute(WIDEN_RULES).rowcount
    print(f"    profiles now reading chassis sensors: {added}")
    print(f"    temperature rules widened past servers: {widened}")


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(DROP_GROUP, {"names": list(NETWORK_PROFILES)})
    bind.execute(NARROW_RULES)
