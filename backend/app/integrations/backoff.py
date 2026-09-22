"""When to try again, and when to stop.

Two rules, and the first one overrides everything else:

1. **`Retry-After` is obeyed verbatim.** Atlassian documents that retrying
   before the header expires returns the same or a LONGER `Retry-After`, so
   guessing earlier is not merely rude, it is slower. A header this precise is
   the server telling us the answer; the schedule below is for when it does
   not.

2. Otherwise exponential with full jitter. The jitter is not decoration: a
   storm enqueues hundreds of rows at once, they all fail against the same
   429, and an unjittered schedule marches the whole batch back at Atlassian
   in lockstep at t+2s, t+4s, t+8s - a self-inflicted thundering herd against
   the very limiter that is already unhappy.

WHICH FAILURES ARE WORTH REPEATING is the other half, and it matters more than
the timing. A 400 from Jira means the payload is wrong - a custom field that
does not exist on this project's create screen, a component nobody created, a
priority name the customer renamed. Retrying that eight times burns eight
requests of a tenant's hourly point quota to get the same answer, and then
buries the useful error under seven duplicates. It goes dead on the first
attempt with the message kept.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

BASE_S = 2.0
FACTOR = 2.0
#: Five minutes. Past that the backoff is no longer about the transient, and a
#: row that has been trying for half an hour needs a human, not more patience.
CAP_S = 300.0
JITTER = (0.7, 1.3)

#: Status codes worth trying again. Everything else is a statement about the
#: request rather than about the moment.
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


def delay_s(attempts: int, *, retry_after: float | None = None,
            rand: random.Random | None = None) -> float:
    """Seconds to wait before attempt number ``attempts + 1``.

    ``attempts`` is the count AFTER the failure, so the first failure passes 1
    and waits about two seconds.
    """
    if retry_after is not None and retry_after >= 0:
        # Not jittered and not capped. It is an instruction, not an estimate,
        # and shortening it is the one thing guaranteed not to help.
        return float(retry_after)
    raw = min(BASE_S * (FACTOR ** max(0, attempts - 1)), CAP_S)
    rng = rand or random
    return raw * rng.uniform(*JITTER)


def next_attempt_at(attempts: int, *, retry_after: float | None = None,
                    now: datetime | None = None,
                    rand: random.Random | None = None) -> datetime:
    return (now or datetime.now(UTC)) + timedelta(
        seconds=delay_s(attempts, retry_after=retry_after, rand=rand))


def is_retryable(status: int | None) -> bool:
    """A transport failure - no status at all - is always worth repeating."""
    return status is None or status in RETRYABLE_STATUS


def parse_retry_after(value: str | None, *, now: datetime | None = None
                      ) -> float | None:
    """Seconds, from either form of the header.

    RFC 9110 allows delay-seconds or an HTTP-date, and Atlassian has been seen
    sending both. A parser that handles only the integer form silently returns
    None on the date form and falls back to the schedule - which usually works
    and occasionally hammers a tenant that asked for a two-minute pause.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())
