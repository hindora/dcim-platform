"""Freshness must never be answered with a count.

`SELECT max(ts), count(*) > 0 FROM telemetry_sample` reads like one cheap
statement. On a 57-million-row hypertable with compressed chunks it is a full
scan: 60,670 ms measured against 4.7 ms for the max on its own.

That statement ran in two places, and both are places where a minute is fatal:

* inside the ingest worker's tick, so the platform monitor stalled the pipeline
  it exists to watch. The worker spent most of every tick waiting on it, ingest
  ran at roughly 60% of the rate the collector was producing, and the estate's
  telemetry fell half an hour behind while the monitor reported itself healthy
  in the gaps between scans;
* in `/ready`, which is a readiness probe that misses its own deadline and has
  an orchestrator restarting a process that was fine.

The two forms answer the same question: a table with no rows has no maximum.
So this is a test about cost, not about behaviour - which is why it reads the
SQL rather than running it.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap

from app.alarms import platform_monitor
from app.api.v1 import misc

# `count(*)` with any spacing, in the same statement as a telemetry scan.
_COUNT = re.compile(r"count\s*\(\s*\*\s*\)")


def _code(fn) -> str:
    """The function's body without its docstring.

    The docstring quotes the statement this test forbids, and the SQL itself is
    a triple-quoted string - so text-splitting on quotes cuts the wrong thing.
    Parsing is the only way to tell the explanation from the code.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    body = node.body[1:] if ast.get_docstring(node) else node.body
    return chr(10).join(ast.unparse(stmt) for stmt in body)


def test_the_monitor_does_not_count_the_hypertable():
    code = _code(platform_monitor._telemetry_freshness)
    assert "telemetry_sample" in code
    assert not _COUNT.search(code), "freshness is counting rows again"


def test_readiness_does_not_count_the_hypertable():
    code = _code(misc.ready)
    assert "telemetry_sample" in code
    telemetry = code[code.index("telemetry_sample") - 400:
                     code.index("telemetry_sample") + 200]
    assert not _COUNT.search(telemetry), "/ready is counting rows again"


async def test_presence_still_follows_the_max():
    """Same answer, without the scan: no maximum means no rows."""
    class _Row(dict):
        def mappings(self):
            return self

        def first(self):
            return self

    class _Session:
        def __init__(self, age):
            self._age = age

        async def execute(self, *_a, **_kw):
            return _Row(age_s=self._age)

    age, present = await platform_monitor._telemetry_freshness(_Session(12.5))
    assert (age, present) == (12.5, True)

    age, present = await platform_monitor._telemetry_freshness(_Session(None))
    assert age is None and present is False
