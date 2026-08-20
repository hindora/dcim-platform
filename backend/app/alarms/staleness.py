"""Reachable-but-silent detection.

An endpoint that stops answering raises endpoint_unreachable and everyone
notices. An endpoint that keeps answering while delivering nothing raises
nothing at all, and that is the worse failure: every dashboard stays green
while the numbers on it quietly age. A hung SNMP agent still completes the
GET. A BMC still authenticates. A mapping that no longer matches the firmware
still walks the tree and comes back empty.

The test is the pair, not either half:

    the poll is succeeding      (endpoint_state.status ONLINE, last_success recent)
    and nothing is arriving     (last_telemetry_at older than the grace period)

Either alone is a different condition. A stale last_telemetry_at on an endpoint
that is also failing to poll is just unreachability, already alarmed, and
raising a second alarm for it would be noise.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

log = get_logger("alarms.staleness")

ALARM_TYPE = "telemetry_stale"

# How many poll intervals of silence before an endpoint is called silent.
# Three, so a single missed poll and its retry do not raise an alarm - the same
# reasoning that makes one failed poll DEGRADED rather than OFFLINE.
GRACE_INTERVALS = 3

# Floor, for fast-polled endpoints. BACnet at 10 s would otherwise alarm after
# 30 s of quiet, which is well inside normal jitter.
MIN_GRACE_S = 300

# Ceiling, so a pathologically long interval cannot make the check useless.
MAX_GRACE_S = 3 * 3600

# Endpoints whose profile has interval 0 are push-driven: a gNMI subscription
# streams on change, so there is no interval to multiply. They get a fixed
# window instead, long enough that a quiet-but-healthy stream is not mistaken
# for a dead one.
PUSH_GRACE_S = 900


def grace_seconds(interval_s: int | None, push_enabled: bool) -> int:
    """How long an endpoint may be silent before it counts as silent."""
    if not interval_s:
        return PUSH_GRACE_S if push_enabled else MIN_GRACE_S
    return max(MIN_GRACE_S, min(MAX_GRACE_S, interval_s * GRACE_INTERVALS))


# Candidates are computed in SQL because the whole fleet is evaluated at once
# and pulling 1400 rows into Python to compare two timestamps would be silly.
#
# `never_reported` is deliberately narrow: an endpoint that has produced
# nothing EVER is only interesting once it has existed long enough to have had
# a fair chance. Without the created_at test, every endpoint would alarm the
# moment it was imported, which trains people to ignore the alarm.
_CANDIDATES = text("""
    SELECT e.id::text                         AS endpoint_id,
           e.device_id::text                  AS device_id,
           d.name                             AS device_name,
           e.protocol::text                   AS protocol,
           e.role::text                       AS role,
           COALESCE(p.interval_s, 0)          AS interval_s,
           COALESCE(p.push_enabled, false)    AS push_enabled,
           st.last_telemetry_at,
           st.last_success,
           EXTRACT(epoch FROM now() - st.last_telemetry_at)::bigint AS silent_s,
           (st.last_telemetry_at IS NULL)     AS never_reported
      FROM device_endpoint e
      JOIN device d            ON d.id = e.device_id
      JOIN endpoint_state st   ON st.endpoint_id = e.id
      LEFT JOIN poll_profile p ON p.id = e.poll_profile_id
     WHERE e.enabled
       AND d.lifecycle <> 'decommissioned'
       -- Reachable: the poll itself is working right now.
       AND st.status = 'ONLINE'
       AND st.last_success > now() - make_interval(secs => :reachable_within)
       -- Silent, or never heard from at all.
       AND (st.last_telemetry_at IS NULL OR st.last_telemetry_at < now())
       AND (st.last_telemetry_at IS NOT NULL
            OR e.created_at < now() - make_interval(secs => :new_endpoint_grace))
""")


async def find_silent(session: AsyncSession, *,
                      reachable_within_s: int = MIN_GRACE_S,
                      new_endpoint_grace_s: int = MAX_GRACE_S,
                      ) -> list[dict[str, Any]]:
    """Endpoints that are polling successfully and delivering nothing."""
    rows = (await session.execute(_CANDIDATES, {
        "reachable_within": reachable_within_s,
        "new_endpoint_grace": new_endpoint_grace_s,
    })).mappings().all()

    out = []
    for r in rows:
        grace = grace_seconds(r["interval_s"], r["push_enabled"])
        silent = r["silent_s"]
        if r["never_reported"] or (silent is not None and silent > grace):
            out.append({**dict(r), "grace_s": grace})
    return out


def message(row: dict[str, Any]) -> str:
    who = f"{row['device_name']} {row['protocol']}/{row['role']}"
    if row["never_reported"]:
        return (f"{who} answers but has never delivered telemetry - "
                f"the poll succeeds and returns nothing to map")
    minutes = (row["silent_s"] or 0) // 60
    return (f"{who} answers but has delivered no telemetry for {minutes} min "
            f"(grace {row['grace_s'] // 60} min)")
