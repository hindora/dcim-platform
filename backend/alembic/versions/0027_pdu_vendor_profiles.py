"""Poll rack PDUs on their vendor's MIB.

Revision ID: 0027
Revises: 0026

80 rack PDUs were polled with `snmp-power-120s`, which asks for the system
group and nothing else, so the meter sitting directly above the IT load
contributed one uptime tick and no power at all. Every other class in the
estate had a live source - CRAH over BACnet, BMCs over Redfish, switchgear
over Modbus - and the PDUs had none.

The mapping now covers PowerNet-MIB rPDU2 (56 APC units) and PDU2-MIB
measurement tables (24 Raritan), which is what a production NMS polls on a rack
PDU. The two MIBs share no OIDs, so the profile follows the VENDOR rather than
the device type: one combined profile would ask every APC unit for Raritan
objects it can only answer noSuchObject to, twice a minute, forever.

Intervals stay at 120 s, matching the rest of the power plane. Rack PDU
metering is a one-second-resolution instrument in the device and a
two-minute-resolution one on the network for a fleet this size; the branch
circuit does not move faster than the load it feeds.

Re-runnable: upserts on profile name, and the endpoint update is idempotent.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

PROFILES = [
    ("snmp-pdu-apc-120s", ["system", "pdu_apc"]),
    ("snmp-pdu-raritan-120s", ["system", "pdu_raritan"]),
]

UPSERT = sa.text("""
    INSERT INTO poll_profile (name, interval_s, timeout_ms, retries, metric_groups)
    VALUES (:name, 120, 3000, 2, :groups)
    ON CONFLICT (name) DO UPDATE SET
        interval_s    = EXCLUDED.interval_s,
        metric_groups = EXCLUDED.metric_groups
""")

# Vendor match is on the name because that is what the importer has to work
# with too, and the two must agree or a re-import silently undoes this.
ASSIGN = sa.text("""
    UPDATE device_endpoint e
       SET poll_profile_id = (SELECT id FROM poll_profile WHERE name = :profile),
           updated_at = now()
      FROM device d
      LEFT JOIN vendor v ON v.id = d.vendor_id
     WHERE d.id = e.device_id
       AND d.device_type = 'pdu'
       AND e.protocol = 'snmp'
       AND lower(coalesce(v.name, '')) LIKE :match
""")

ASSIGNMENTS = [
    ("snmp-pdu-apc-120s", "%apc%"),
    ("snmp-pdu-apc-120s", "%schneider%"),
    ("snmp-pdu-raritan-120s", "%raritan%"),
]


def upgrade() -> None:
    bind = op.get_bind()
    for name, groups in PROFILES:
        bind.execute(UPSERT, {"name": name, "groups": groups})
    for profile, match in ASSIGNMENTS:
        bind.execute(ASSIGN, {"profile": profile, "match": match})


def downgrade() -> None:
    bind = op.get_bind()
    # Back to the generic power profile first: the profiles cannot be deleted
    # while endpoints still reference them.
    bind.execute(sa.text("""
        UPDATE device_endpoint e
           SET poll_profile_id = (SELECT id FROM poll_profile
                                   WHERE name = 'snmp-power-120s'),
               updated_at = now()
          FROM poll_profile p
         WHERE p.id = e.poll_profile_id
           AND p.name IN ('snmp-pdu-apc-120s', 'snmp-pdu-raritan-120s')
    """))
    bind.execute(sa.text("""
        DELETE FROM poll_profile
         WHERE name IN ('snmp-pdu-apc-120s', 'snmp-pdu-raritan-120s')
    """))
