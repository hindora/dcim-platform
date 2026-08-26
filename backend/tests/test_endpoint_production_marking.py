"""An endpoint that delivers ANY sample has delivered telemetry.

`telemetry_stale` asks a simple question - this endpoint answers, so where is
its data? - and it answers it from `produced`, the per-endpoint map the ingest
worker fills as samples land. That map used to be filled in the gauge branch
only, and every other value type returns early:

    text    -> continue
    bool    -> continue
    counter -> continue
    gauge   -> ... produced[endpoint] = observed

So an endpoint whose entire output is counters was written to the database on
every cycle and still counted as having produced nothing. That is not a corner
case here. `sys_uptime` is a COUNTER and it is the only metric the `system`
group yields, so all 486 endpoints on a system-only profile - every BMC and
every facility card in the estate - held a permanent `telemetry_stale` while
their samples arrived on time, 92 seconds old when measured. Bool-only
endpoints, which is what a BACnet fault-point endpoint is, had the same fate.

The rule was reporting a blind spot in the ingest path as an equipment
condition, and nothing done to the equipment could have cleared it.

This is a STRUCTURAL test. Exercising `_handle_telemetry` end to end needs a
database, a Redis and a populated inventory cache; the property that actually
matters is positional - the marking has to dominate the value-type dispatch
rather than live inside one of its branches - and position is exactly what an
AST can check and a mocked call cannot.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from app.ingest import worker as worker_mod


def _sample_loop() -> ast.For:
    """The `for s in samples:` loop inside `_handle_telemetry`."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(
        worker_mod.IngestWorker._handle_telemetry)))
    for node in ast.walk(tree):
        if (isinstance(node, ast.For) and isinstance(node.target, ast.Name)
                and node.target.id == "s"
                and any("produced" in ast.dump(stmt) for stmt in node.body)):
            return node
    raise AssertionError("no sample loop that marks production")


def _marks_production(stmt: ast.stmt) -> bool:
    for node in ast.walk(stmt):
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == "produced"
                and isinstance(node.ctx, ast.Store)):
            return True
    return False


def _dispatches_on_value_type(stmt: ast.stmt) -> bool:
    return (isinstance(stmt, ast.If)
            and "ValueType" in ast.dump(stmt.test)
            and "value_type" in ast.dump(stmt.test))


def test_production_is_marked_before_the_value_type_is_dispatched():
    loop = _sample_loop()
    marks = [i for i, stmt in enumerate(loop.body) if _marks_production(stmt)]
    dispatches = [i for i, stmt in enumerate(loop.body)
                  if _dispatches_on_value_type(stmt)]

    assert marks, "nothing marks the endpoint as having produced"
    assert dispatches, "the value-type dispatch moved; this test needs rewriting"
    assert min(marks) < min(dispatches), (
        "production is marked after the value type is dispatched - every branch "
        "that returns early will count as delivering nothing")


def test_production_is_not_marked_inside_one_value_type_branch():
    """The regression itself: the marking living inside `if gauge:`."""
    loop = _sample_loop()
    for stmt in loop.body:
        if _dispatches_on_value_type(stmt) and _marks_production(stmt):
            raise AssertionError(
                "an endpoint's production is recorded inside a value-type "
                "branch, so counter-only and bool-only endpoints will read as "
                "stale forever")


def test_every_value_type_still_returns_early():
    """Why the marking has to come first, pinned so it stays true.

    If the branches ever stop returning early the ordering matters less - but
    while they do, anything after them is unreachable for most samples.
    """
    loop = _sample_loop()
    early = [stmt for stmt in loop.body
             if _dispatches_on_value_type(stmt)
             and any(isinstance(n, ast.Continue) for n in ast.walk(stmt))]
    assert len(early) >= 2, (
        "expected the text/bool/counter branches to continue; the ingest loop "
        "has been restructured and this reasoning needs revisiting")
