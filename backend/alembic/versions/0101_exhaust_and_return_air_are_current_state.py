"""Exhaust and return air are current state.

A server's exhaust and a CRAH's return air were collected - thousands of
samples an hour - but marked not-hot, so the ingest worker never copied them
into `device_state.metrics`. Every reader of current state therefore saw them
as missing: the room viewer's rear rack faces stayed grey, SHI/RHI and the
hot-aisle heat map had no inputs, and RTI was blank because no air handler
had a return. Found on the live estate on 2026-10-08.

The registry (contracts/metrics/registry.yaml) now marks both hot; this
brings the `metric` rows the worker reads in line. The worker reads the flag
at start, so it has to be restarted for the change to take.

Revision ID: 0101
Revises: 0100
"""

from __future__ import annotations

from alembic import op

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None

_KEYS = ("exhaust_temperature", "return_air_temp")


def upgrade() -> None:
    op.execute(
        "UPDATE metric SET is_hot = true WHERE key IN ("
        + ", ".join(f"'{k}'" for k in _KEYS) + ")")


def downgrade() -> None:
    op.execute(
        "UPDATE metric SET is_hot = false WHERE key IN ("
        + ", ".join(f"'{k}'" for k in _KEYS) + ")")
