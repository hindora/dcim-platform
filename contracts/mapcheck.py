#!/usr/bin/env python3
"""Validate every protocol mapping against the metric registry.

A mapping that names a metric the registry does not define would silently drop
that metric at runtime - the collector refuses to emit unknown keys. Catching it
here makes it a CI failure instead of a missing chart nobody notices for a week.

    python contracts/mapcheck.py
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: pip install pyyaml")

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "contracts"

VALUE_TYPES = {"gauge", "counter", "delta", "bool", "text"}


def load(p: Path) -> dict:
    with p.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# Keys that name a metric. Redfish picks between an rpm and a percent metric
# from the sensor's own units, so it carries two.
METRIC_KEYS = ("metric", "metric_rpm", "metric_pct")


def walk_metric_refs(node, path: str = ""):
    """Yield (metric_key, value_type|None, where) for every mapping entry."""
    if isinstance(node, dict):
        for key in METRIC_KEYS:
            if key in node and isinstance(node[key], str):
                yield node[key], node.get("value_type"), f"{path}.{key}"
        for k, v in node.items():
            yield from walk_metric_refs(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk_metric_refs(v, f"{path}[{i}]")


def main() -> int:
    registry = load(CONTRACTS / "metrics" / "registry.yaml")
    defs = {m["key"]: m for m in registry["metrics"]}

    errors: list[str] = []
    checked = 0
    files = sorted((CONTRACTS / "mappings").rglob("*.yaml"))
    if not files:
        print("no mapping files found", file=sys.stderr)
        return 1

    for f in files:
        doc = load(f)
        rel = f.relative_to(ROOT)
        for key, vtype, where in walk_metric_refs(doc):
            checked += 1
            d = defs.get(key)
            if d is None:
                errors.append(f"{rel}{where}: unknown metric '{key}'")
                continue
            if vtype is None:
                continue
            if vtype not in VALUE_TYPES:
                errors.append(f"{rel}{where}: bad value_type '{vtype}'")
            elif vtype != d["value_type"]:
                errors.append(
                    f"{rel}{where}: '{key}' declared {vtype} here but "
                    f"{d['value_type']} in the registry")

    if errors:
        print(f"mapcheck FAILED ({len(errors)} problem(s)):", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    print(f"mapcheck OK - {checked} metric references across {len(files)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
