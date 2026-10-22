"""The threshold rules never judge a sample the instrument has disowned.

Since the collector reads BACnet Status_Flags and Reliability, a point in fault
arrives as quality BAD while still carrying the value it last held. Feeding that
held value to the rules would raise - or clear - a process alarm on a number the
device itself says is not to be trusted. A sensor fault is a data-quality
finding (the anomaly detector's DQ checks), not a hot aisle.
"""

from __future__ import annotations

import ast
import inspect

from app.ingest import worker
from app.ingest.worker import feeds_rules


def test_a_bad_sample_does_not_reach_the_rules():
    assert feeds_rules("bad") is False


def test_good_and_suspect_samples_still_do():
    # SUSPECT is out of range, out of service or overridden: still evidence of a
    # kind, and narrowing it is a separate decision from this one.
    assert feeds_rules("good") is True
    assert feeds_rules("suspect") is True


def test_every_rule_input_is_gated_on_quality():
    """A guard on two of the three feeds is a guard with a hole in it: the
    boolean points, the gauges and the derived rack load all reach the rules."""
    tree = ast.parse(inspect.getsource(worker))
    appends = []
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "rule_inputs"):
            appends.append(node)
    assert len(appends) >= 3, "expected the boolean, gauge and load_pct feeds"
    for call in appends:
        node, gated = call, False
        while id(node) in parents:
            node = parents[id(node)]
            if isinstance(node, ast.If) and "feeds_rules" in ast.unparse(node.test):
                gated = True
                break
        assert gated, f"rule_inputs.append at line {call.lineno} is not behind feeds_rules()"
