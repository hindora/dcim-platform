"""A collector can check itself before it starts polling.

docs/26 Phase 8. The operator journey this phase targets - bring up a
site's collection in the UI, no shell, no runbook - needs somewhere for
`dcim-collector preflight`'s findings to land: NTP offset, spool disk
space, whether the configured trap port is actually bindable, and TLS
reachability to this platform. A one-shot check that only ever prints to a
terminal is invisible to the onboarding wizard the frontend half of this
phase will build; this table is what lets that wizard poll "did preflight
pass" instead of asking someone to read a collector's own log.

History, not just the latest run: `collector_preflight_result` keeps one
row per run rather than overwriting a single row on `collector_instance` -
comparing THIS install's preflight against a rerun after fixing a firewall
rule is exactly the kind of thing an operator reruns preflight to check,
and only history makes that comparison possible.

Revision ID: 0091
Revises: 0090
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collector_preflight_result",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        # No FK to collector_instance: preflight runs at enroll time, which
        # can be before the row exists in every ordering an operator might
        # actually use (a scripted install racing the create-collector API
        # call). A result naming a collector nobody has heard of yet is
        # still worth keeping - the alternative is silently discarding it.
        sa.Column("collector_id", sa.Text, nullable=False),
        # clock_timestamp(), not now(): now() is fixed for the whole
        # transaction it runs in, and a caller inserting two runs for the
        # same collector inside one transaction - exactly what this
        # migration's own tests do - would see both land with an
        # identical, frozen ran_at, making "newest first" undefined between
        # them. Same bug, same fix, as migration 0089's healthy_since.
        sa.Column("ran_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        # true only if every check that ran reported ok - a check that could
        # not run at all (e.g. no pool assigned yet) is neither pass nor
        # fail and must not silently count as a pass.
        sa.Column("passed", sa.Boolean, nullable=False),
        # [{"check": "ntp_offset", "status": "ok"|"warn"|"fail"|"skipped",
        #   "detail": "...", "value": <number, optional>}, ...] - open-ended
        # on purpose, so a new check type never needs a migration to add.
        sa.Column("checks", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
    )
    op.create_index("ix_collector_preflight_result_collector", "collector_preflight_result",
                    ["collector_id", "ran_at"])


def downgrade() -> None:
    op.drop_table("collector_preflight_result")
