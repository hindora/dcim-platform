"""The nightly step: baseline fits (Phase 3) and the retention prune.

None of the anomaly tables is a hypertable, so retention is not a Timescale
policy but this prune (docs/29 section 6.3), run after the fit under the
DCIMB session lock. Each table is pruned in batches of PRUNE_BATCH rows, one
short transaction per batch, so no transaction holds a snapshot open for
long on a host that already swaps under Timescale jobs.

| table                      | deleted when                                       |
| anomaly_finding            | resolved more than 2 years ago (alarm_history's 2y)|
| anomaly_incident           | resolved more than 2 years ago                     |
| anomaly_baseline           | superseded more than 30 days ago                   |
| anomaly_state              | not updated for 7 days (the series stopped)        |
| anomaly_baseline_exclusion | ended more than 2 years ago                        |
| anomaly_override           | expired more than 7 days ago                       |

Incidents are pruned after findings: a finding's incident FK is SET NULL,
and its own two-year clock is the same, so the order only saves updates.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.logging import get_logger
from app.repositories import anomalies as repo

log = get_logger("anomaly.baselines")

PRUNE_BATCH = 5_000
#: A runaway table must not keep the nightly step busy forever.
MAX_BATCHES_PER_TABLE = 200


async def prune(conn: Any, now: datetime, *, batch: int = PRUNE_BATCH) -> dict[str, int]:
    """Batched retention prune on a dedicated connection. Returns rows per table."""
    out: dict[str, int] = {}
    for table, key, predicate in repo.PRUNE_RULES:
        total = 0
        for _ in range(MAX_BATCHES_PER_TABLE):
            n = await repo.prune_batch(conn, table, key, predicate, now=now, batch=batch)
            await conn.commit()
            total += n
            if n < batch:
                break
        out[table] = total
    if any(out.values()):
        log.info("anomaly prune", **out)
    return out
