"""Thirty days of raw telemetry, not ninety.

The disk filled. The raw sample hypertables were set to keep 90 days
(samples) and 180 days (booleans, text) back when the estate was a few
dozen devices; at 1,386 endpoints reporting every poll they run to tens of
gigabytes a month, and the Docker data disk on a developer machine ran out
with the operating system underneath it.

Thirty days is what a raw sample is for: the last few weeks at full
resolution, to read a specific incident back. Anything older is answered
by the rollups - `telemetry_5m` keeps its two-year window, it is a fraction
of the size, and the long-window charts already read from it (0042; 0052
cuts `telemetry_1m` to a week, it was no smaller than raw). The event stream follows the raw
samples to 30 days for the same reason; alarm history is not a hypertable
with a policy and is untouched.

`poll_result` already kept 14 days and stays there.

Revision ID: 0051
Revises: 0050
"""

from __future__ import annotations

from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None

#: (hypertable, new window, the window it had) - the old value is what the
#: downgrade restores, so it is written down here rather than remembered.
POLICIES = (
    ("telemetry_sample", "30 days", "90 days"),
    ("telemetry_bool", "30 days", "180 days"),
    ("telemetry_text", "30 days", "180 days"),
    ("event", "30 days", "90 days"),
)


def _set(table: str, window: str) -> None:
    # Timescale has no ALTER for a policy: drop it and add it back. if_exists
    # keeps a fresh database (no policy yet) from failing the upgrade.
    op.execute(f"SELECT remove_retention_policy('{table}', if_exists => TRUE)")
    op.execute(f"SELECT add_retention_policy('{table}', INTERVAL '{window}')")


def upgrade() -> None:
    for table, window, _ in POLICIES:
        _set(table, window)


def downgrade() -> None:
    for table, _, window in POLICIES:
        _set(table, window)
