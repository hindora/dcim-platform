"""Merge the open alarms that two detectors raised for one condition.

Revision ID: 0030
Revises: 0029

The trap path filed its own vocabulary as the alarm type, so a pinned CPU held
`cpu_high_usage` from the trap and `cpu_high` from the poll rule at the same
time, on the same device, for the same fact. Same for `cpu_sustained` against
`cpu_saturated` and `memory_high_usage` against `memory_high`.

The raise path is fixed and now files everything under the canonical name.
These are the rows that were already open when it changed - and they will not
resolve themselves: the trap's recovery clears the canonical name now, so the
old row would sit open until somebody noticed it by hand.

Two cases, and the second is the one worth being careful about:

* the canonical row does NOT exist - rename the old row and keep everything,
  including its first_seen. The condition started when the trap said so.
* BOTH exist - keep the canonical row, take the WORSE severity of the two and
  the EARLIER first_seen, add the occurrence counts, and clear the alias row
  with history saying why. Deleting it would lose the fact that a trap fired;
  leaving it open would leave the duplicate this migration exists to remove.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

# alias -> canonical. Mirrors CANONICAL_ALARM_TYPE; the literals are pinned
# here on purpose, because a migration that imports live code changes meaning
# the next time that code is edited.
ALIASES = {
    "cpu_high_usage": "cpu_high",
    "cpu_sustained": "cpu_saturated",
    "memory_high_usage": "memory_high",
}

RANK = ("CASE {col}::text WHEN 'CRITICAL' THEN 0 WHEN 'MAJOR' THEN 1 "
        "WHEN 'MINOR' THEN 2 WHEN 'WARNING' THEN 3 WHEN 'INFO' THEN 4 ELSE 5 END")

# Fold the alias into the canonical row where both are open.
FOLD = sa.text(f"""
    WITH pair AS (
        SELECT alias.id AS alias_id, canon.id AS canon_id,
               alias.severity AS alias_sev, canon.severity AS canon_sev,
               alias.first_seen AS alias_first, canon.first_seen AS canon_first,
               alias.occurrence_count AS alias_n, canon.device_id AS device_id
        FROM alarm alias
        JOIN alarm canon
          ON canon.device_id = alias.device_id
         AND canon.instance IS NOT DISTINCT FROM alias.instance
         AND canon.alarm_type = :canonical
         AND canon.state <> 'CLEARED'
        WHERE alias.alarm_type = :alias
          AND alias.state <> 'CLEARED'
    ), merged AS (
        UPDATE alarm a
           SET severity = CASE
                   WHEN {RANK.format(col='p.alias_sev')}
                      < {RANK.format(col='p.canon_sev')}
                   THEN p.alias_sev ELSE p.canon_sev END,
               first_seen = LEAST(p.canon_first, p.alias_first),
               occurrence_count = a.occurrence_count + p.alias_n
          FROM pair p
         WHERE a.id = p.canon_id
        RETURNING a.id
    ), logged AS (
        INSERT INTO alarm_history (alarm_id, device_id, action, severity,
                                   actor, detail)
        SELECT p.alias_id, p.device_id, 'clear', p.alias_sev, 'migration:0030',
               '{{"reason": "merged into the canonical alarm type"}}'
        FROM pair p
        RETURNING alarm_id
    )
    UPDATE alarm SET state = 'CLEARED', cleared_at = now()
     WHERE id IN (SELECT alias_id FROM pair)
""")

# Where only the alias is open, the row simply takes the canonical name.
RENAME = sa.text("""
    UPDATE alarm
       SET alarm_type = :canonical
     WHERE alarm_type = :alias
       AND state <> 'CLEARED'
""")


def upgrade() -> None:
    bind = op.get_bind()
    for alias, canonical in ALIASES.items():
        # Fold first: afterwards no alias row that collides is still open, so
        # the rename cannot violate the one-open-alarm-per-key index.
        bind.execute(FOLD, {"alias": alias, "canonical": canonical})
        bind.execute(RENAME, {"alias": alias, "canonical": canonical})


def downgrade() -> None:
    # Deliberately empty. The merge is lossy by design - two rows became one,
    # and the counts were added together - so re-splitting them would invent an
    # alarm rather than restore one. The alias rows this cleared are still in
    # alarm_history with the reason attached.
    pass
