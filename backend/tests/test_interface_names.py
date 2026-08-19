"""The interface-normalisation rule, checked against the shared vectors.

The rule is implemented twice - here in Python for the ingest worker, and in
Go for the collector. Two implementations of one rule drift, and the failure
is silent: one port quietly becomes two series again. Both sides read
contracts/testdata/interface_names.json, so a change to either has to be a
change to both.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ingest.interfaces import InterfaceIndex, interface_key, interface_name

VECTORS = json.loads(
    (Path(__file__).resolve().parents[2] / "contracts" / "testdata"
     / "interface_names.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["in"] or "empty")
def test_shared_vectors(case):
    assert interface_name(case["in"]) == case["name"]
    assert interface_key(case["in"]) == case["key"]


@pytest.mark.parametrize("pair", VECTORS["same"], ids=lambda p: f"{p[0]}~{p[1]}")
def test_names_that_mean_one_port(pair):
    assert interface_key(pair[0]) == interface_key(pair[1])


@pytest.mark.parametrize("pair", VECTORS["distinct"], ids=lambda p: f"{p[0]}!={p[1]}")
def test_names_that_are_different_ports(pair):
    assert interface_key(pair[0]) != interface_key(pair[1])


def test_index_resolves_every_form_to_inventorys_name():
    index = InterfaceIndex([("GigabitEthernet0/1", 2), ("Ethernet1", 1)])

    # The short form, the wrong case, and the ifIndex all name one port, and
    # the answer is always what inventory calls it.
    for probe in ("GigabitEthernet0/1", "Gi0/1", "gi0/1", "2"):
        assert index.resolve(probe) == "GigabitEthernet0/1", probe

    assert index.resolve("Ethernet1") == "Ethernet1"
    assert index.resolve("1") == "Ethernet1"


def test_index_returns_none_for_things_that_are_not_ports():
    index = InterfaceIndex([("GigabitEthernet0/1", 2)])
    assert index.resolve("PSU1") is None
    assert index.resolve("") is None
    assert index.resolve("CHASSIS") is None


def test_a_real_name_wins_over_an_ifindex_collision():
    """A port literally named "2" must not be shadowed by another's ifIndex.

    Numeric names are rare but legal, and inventory's own name is the stronger
    claim - so it is inserted first and the ifIndex alias never overwrites it.
    """
    index = InterfaceIndex([("2", 7), ("GigabitEthernet0/1", 2)])
    assert index.resolve("2") == "2"
