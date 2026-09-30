"""A collector's capacity report (docs/26 Phase 5), decoded once.

The heartbeat carries it as a JSON string (contracts: CollectorHeartbeat.
capacity, tag 26) over both transports - the Redis stream the ingest worker
reads and the HTTP fallback a gateway-transport collector POSTs - and both
store it as an object through this one function, so the two cannot drift.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.logging import get_logger

log = get_logger("collector_capacity")


def decode(raw: Any, collector_id: str = "") -> dict[str, Any] | None:
    """None - not {} - when absent or unreadable: an old collector, or one
    with a single reading so far, has reported nothing, and every reader
    treats None as "not reported" rather than as a collector doing no work."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        log.warning("collector sent unreadable capacity report",
                    collector_id=collector_id)
        return None
    return parsed if isinstance(parsed, dict) else None
