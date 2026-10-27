"""A network box is given ten seconds to answer, not three.

The two 600 s network profiles asked for the system group, the whole
interface table and the host or Cisco CPU objects with a 3 s per-request
timeout. gosnmp applies that timeout to every GETBULK of the walk, and the
scheduler gives the poll as a whole timeout x (retries + 1) + 1 s, so the
entire walk had ten seconds. A 48-port switch answers that in well under a
second from a healthy agent, but a control plane busy with a reconvergence,
a chassis with hundreds of interfaces, or an agent that is simply slow (the
simulator's responder under memory pressure took 400-870 ms per request,
measured) tips over, the walk aborts, and the device reads as DEGRADED for
the next ten minutes.

On a 600 s interval nothing is gained by refusing to wait. Ten seconds per
request is what an operator sets on a slow control plane in practice
(Zabbix and SolarWinds both allow up to 30 s), and keeps the poll budget at
31 s against a 600 s interval - still under a twentieth of the interval.

Applied only where the value is still the seed's 3000, so an operator's own
edit through the poll-profile form is never overwritten. The downgrade puts
3000 back where this migration put 10000.

Revision ID: 0114
Revises: 0113
"""

from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None

PROFILES = ["snmp-network-cisco-600s", "snmp-network-host-600s"]
OLD_TIMEOUT_MS = 3000
NEW_TIMEOUT_MS = 10_000


def upgrade() -> None:
    op.get_bind().execute(text("""
        UPDATE poll_profile
           SET timeout_ms = :new
         WHERE name = ANY(:names)
           AND timeout_ms = :old
    """), {"new": NEW_TIMEOUT_MS, "old": OLD_TIMEOUT_MS, "names": PROFILES})


def downgrade() -> None:
    op.get_bind().execute(text("""
        UPDATE poll_profile
           SET timeout_ms = :old
         WHERE name = ANY(:names)
           AND timeout_ms = :new
    """), {"new": NEW_TIMEOUT_MS, "old": OLD_TIMEOUT_MS, "names": PROFILES})
