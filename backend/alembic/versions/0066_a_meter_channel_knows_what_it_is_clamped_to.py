"""A CT channel records which breaker it is clamped to.

Revision ID: 0066
Revises: 0065

A branch-circuit monitor ships with its channels called Ckt01..Ckt42 and
nothing else. Which breaker each CT is on is programmed in at commissioning,
and every real monitor stores it - Veris E30, Packet Power, Verdigris - because
that schedule is the only thing that turns forty-two anonymous numbers into
"this panel is feeding that rack".

Without it this platform could read every branch on every meter and attribute
none of them. The clearest cost was the transfer switches: ATS1-DC1-UR is
metered at 102 kW on channel 1 of the utility board's meter, and the product
had to fall back to adding up the loads underneath it instead - an inference,
published beside measurements, and a different number.

WHAT THIS TABLE IS. The commissioning schedule, imported from the meters
rather than typed in: (meter, channel) -> the device that channel measures. It
is NOT telemetry and carries no reading. A reading is a sample against the
meter, keyed by the channel's instance; this says whose reading it is.

WHY IT IS ITS OWN TABLE. A channel is not a property of either device. The
meter does not own it - a CT gets moved to another breaker and the meter is
unchanged; the branch does not own it - it can be metered by nobody, or by a
second monitor on the other side of a 2N pair. It is the relationship, and it
has its own lifetime: `source` and `discovered_at` say where the schedule came
from and when, because a schedule that was true six months ago is exactly how
a DCIM ends up confidently attributing a load to the wrong rack.
"""

from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE meter_channel (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            -- The meter the channel belongs to, and the instance its samples
            -- carry. Together they are what a reading is keyed by, so the
            -- join back to telemetry needs nothing else.
            meter_device_id uuid NOT NULL REFERENCES device(id) ON DELETE CASCADE,
            instance        text NOT NULL,
            -- What the CT is clamped to. NULL is a spare way that the meter
            -- reported as spare: known, and known to be nothing, which is not
            -- the same as a channel nobody has commissioned.
            branch_device_id uuid REFERENCES device(id) ON DELETE SET NULL,
            -- The label exactly as the meter gave it, kept even when it did
            -- not resolve to a device. An unresolved label is the evidence
            -- that a schedule exists and this platform disagrees with it.
            label           text,
            -- 'bacnet' - read from the meter's own object descriptions.
            -- Anything else a later import adds (a CSV panel schedule, a
            -- manual entry) says so here rather than being indistinguishable.
            source          text NOT NULL DEFAULT 'bacnet',
            discovered_at   timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT meter_channel_uniq UNIQUE (meter_device_id, instance)
        )
    """)
    # The read path is "every channel of this meter" and "who meters this
    # branch", in that order of frequency.
    op.execute("CREATE INDEX ix_meter_channel_meter ON meter_channel (meter_device_id)")
    op.execute("""CREATE INDEX ix_meter_channel_branch ON meter_channel (branch_device_id)
                  WHERE branch_device_id IS NOT NULL""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS meter_channel")
