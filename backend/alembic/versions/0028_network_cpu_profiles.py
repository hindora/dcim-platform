"""Poll network gear for the CPU and memory its NOS actually publishes.

Revision ID: 0028
Revises: 0027

98 switches, routers, firewalls, load balancers and OOB switches were on
`snmp-network-600s`, which asks for the system group and the interface tables.
Neither carries control-plane load, and HOST-RESOURCES - the MIB the platform
reads CPU from on a server - is a HOST MIB that network agents do not serve.
`hrProcessorLoad` on a spine returns noSuchInstance, measured.

So the number that decides whether a box still answers a keepalive reached this
platform only as a trap: nothing to graph, nothing to threshold against, and
nothing at all for the 12 firewalls and 4 load balancers, which speak no gNMI
either - PAN-OS exposes an XML/REST API and F5 TMOS exposes iControl REST.
A CPU fault injected on a firewall was visible as a trap and in no other way.

Two profiles, because two MIB families that share no OIDs:

  cisco  CISCO-PROCESS-MIB cpmCPUTotal5minRev + CISCO-MEMORY-POOL-MIB, which is
         what an NMS polls on IOS and NX-OS. 50 devices.
  host   HOST-RESOURCES + UCD, which the Linux-based NOSes genuinely serve -
         Dell OS10, PAN-OS, F5 TMOS, Arista EOS. 48 devices.

A single combined profile would ask every Cisco box for host MIBs it does not
carry and every Dell box for Cisco objects it does not carry, on every poll,
forever.

The 600 s interval is unchanged. Control-plane CPU is a five-minute average on
the Cisco side by definition; polling it faster samples the same number twice.

Re-runnable: upserts on profile name, assignment is idempotent.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

PROFILES = [
    ("snmp-network-cisco-600s", ["system", "interfaces", "network_cisco"]),
    ("snmp-network-host-600s", ["system", "interfaces", "host_resources"]),
]

UPSERT = sa.text("""
    INSERT INTO poll_profile (name, interval_s, timeout_ms, retries, metric_groups)
    VALUES (:name, 600, 3000, 2, :groups)
    ON CONFLICT (name) DO UPDATE SET
        interval_s    = EXCLUDED.interval_s,
        metric_groups = EXCLUDED.metric_groups
""")

NETWORK_TYPES = ["switch", "router", "firewall", "load_balancer", "oob_switch"]

# Cisco first, then everything else that is still on the old generic profile -
# so a vendor nobody has mapped lands on the host MIBs, which is the correct
# guess for any modern NOS and produces a clean noSuchObject rather than a
# wrong reading if it turns out not to be.
ASSIGN_CISCO = sa.text("""
    UPDATE device_endpoint e
       SET poll_profile_id = (SELECT id FROM poll_profile
                               WHERE name = 'snmp-network-cisco-600s'),
           updated_at = now()
      FROM device d
      LEFT JOIN vendor v ON v.id = d.vendor_id
     WHERE d.id = e.device_id
       AND e.protocol = 'snmp'
       AND d.device_type = ANY(:types)
       AND lower(coalesce(v.name, '')) LIKE '%cisco%'
""")

ASSIGN_HOST = sa.text("""
    UPDATE device_endpoint e
       SET poll_profile_id = (SELECT id FROM poll_profile
                               WHERE name = 'snmp-network-host-600s'),
           updated_at = now()
      FROM device d
      LEFT JOIN vendor v ON v.id = d.vendor_id
     WHERE d.id = e.device_id
       AND e.protocol = 'snmp'
       AND d.device_type = ANY(:types)
       AND lower(coalesce(v.name, '')) NOT LIKE '%cisco%'
""")

REVERT = sa.text("""
    UPDATE device_endpoint e
       SET poll_profile_id = (SELECT id FROM poll_profile
                               WHERE name = 'snmp-network-600s'),
           updated_at = now()
      FROM poll_profile p
     WHERE p.id = e.poll_profile_id
       AND p.name IN ('snmp-network-cisco-600s', 'snmp-network-host-600s')
""")


def upgrade() -> None:
    bind = op.get_bind()
    for name, groups in PROFILES:
        bind.execute(UPSERT, {"name": name, "groups": groups})
    bind.execute(ASSIGN_CISCO, {"types": NETWORK_TYPES})
    bind.execute(ASSIGN_HOST, {"types": NETWORK_TYPES})


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(REVERT)
    bind.execute(sa.text("""
        DELETE FROM poll_profile
         WHERE name IN ('snmp-network-cisco-600s', 'snmp-network-host-600s')
    """))
