"""Pacing outbound calls, so the limiter is something we stay under rather
than something we discover.

Jira Cloud runs three independent limiters and all of them answer 429:

* an hourly POINTS quota, per tenant, whose size depends on the edition and
  the user count - so it cannot be computed from here, only respected;
* a per-second BURST limit, roughly 100 RPS for GET and POST;
* a PER-ISSUE WRITE limit: 20 writes in 2 seconds, 100 in 30.

The third is the one a DCIM meets first, and it is not obvious why until you
picture a flapping condition: every occurrence wants to touch the same issue,
so the whole estate's traffic converges on one key. Hence two buckets - a
global one for the endpoint limits and a per-issue one for that - and hence
the design decision upstream that a repeat occurrence updates custom fields
rather than adding a comment, because one write beats two.

Deliberately in-process and not in Redis. Two ingest workers each pace
themselves, so the real ceiling is twice the configured rate; at the default
of 5 RPS that is 10, still an order of magnitude under the burst limit, and
the alternative - a Redis round trip before every HTTP call, on the path that
runs hardest during a storm - buys precision nobody needs and adds a
dependency to the one code path that most needs to keep working.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict

#: Per-issue ceiling, well under Jira's documented 20-per-2-seconds. The margin
#: is deliberate: two workers pace independently, so the fleet-wide rate is
#: twice this, and 10-in-2-seconds still leaves half the allowance to whatever
#: else is touching that issue - an automation rule, a human typing.
PER_ISSUE_PER_2S = 5

#: How many issue keys keep their own bucket. An LRU rather than an unbounded
#: dict, because the key space is every issue this platform has ever touched
#: and a per-key bucket that is never evicted is a slow memory leak wearing a
#: rate limiter's clothes.
MAX_TRACKED_ISSUES = 2048


class TokenBucket:
    """Classic token bucket, refilled continuously.

    Continuous refill rather than per-interval top-up so a burst does not sit
    waiting for a window boundary that has no meaning to the server.
    """

    def __init__(self, rate_per_s: float, capacity: float | None = None) -> None:
        self.rate = max(0.001, float(rate_per_s))
        # Capacity equal to one second of rate: enough to absorb a small
        # clump, not enough to save up a storm's worth of allowance and spend
        # it in one breath, which is exactly what a burst limiter punishes.
        self.capacity = float(capacity if capacity is not None else max(1.0, self.rate))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    async def take(self, tokens: float = 1.0) -> float:
        """Wait until a token is available. Returns how long it waited."""
        waited = 0.0
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                sleep_s = deficit / self.rate
            # Slept OUTSIDE the lock. Holding it across the sleep serialises
            # every waiter behind the first one's full delay, which turns a
            # rate limiter into a queue with the wrong throughput.
            await asyncio.sleep(sleep_s)
            waited += sleep_s


class Limiter:
    """The global bucket plus one small bucket per issue key."""

    def __init__(self, rate_per_s: float) -> None:
        self.overall = TokenBucket(rate_per_s)
        self._issues: OrderedDict[str, TokenBucket] = OrderedDict()

    def _issue_bucket(self, issue_key: str) -> TokenBucket:
        bucket = self._issues.pop(issue_key, None)
        if bucket is None:
            bucket = TokenBucket(PER_ISSUE_PER_2S / 2.0, capacity=PER_ISSUE_PER_2S)
        self._issues[issue_key] = bucket
        while len(self._issues) > MAX_TRACKED_ISSUES:
            self._issues.popitem(last=False)
        return bucket

    async def acquire(self, issue_key: str | None = None) -> float:
        """Permission for one outbound write. Returns seconds waited."""
        waited = await self.overall.take()
        if issue_key:
            waited += await self._issue_bucket(issue_key).take()
        return waited
