"""Rack roll-up: a hall's leaf equipment collapsed into one node per rack.

The shape under test is one rack of a real hall:

    RPPA --> PDUA --+-> srv1 .. srv4      (leaves, same rack)
                    +-> sensor1, sensor2  (leaves, same rack, other type)

PDUA is in the rack too and must NOT collapse: the chain runs through it, and
hiding it hides the hop the whole diagram is about.
"""

from __future__ import annotations

from app.schemas import LocationRef, TopologyEdge, TopologyNode
from app.services.topology import _rollup_by_rack

RACK = LocationRef(room_id="room1", room_name="Hall A",
                   rack_id="r1", rack_name="R01")


def node(nid: str, device_type: str, *, status: str = "ONLINE",
         severity: str = "CLEAR", rack: LocationRef | None = RACK,
         metrics: dict[str, float] | None = None) -> TopologyNode:
    return TopologyNode(
        id=nid, name=nid.upper(), device_type=device_type, status=status,
        max_severity=severity, location=rack or LocationRef(),
        metrics=metrics or {})


def edge(eid: str, source: str, target: str, *, side: str | None = "A",
         oper: str = "up") -> TopologyEdge:
    return TopologyEdge(id=eid, source=source, target=target, layer="power",
                        redundancy_side=side, oper_state=oper)


def build():
    nodes = [
        node("rppa", "rpp", rack=LocationRef(room_id="room1", room_name="Hall A")),
        node("pdua", "pdu"),
        *[node(f"srv{i}", "server", metrics={"power_w": 400.0,
                                             "inlet_temp_c": 20.0 + i})
          for i in range(1, 5)],
        node("sensor1", "sensor"),
        node("sensor2", "sensor"),
    ]
    edges = [
        edge("e0", "rppa", "pdua"),
        *[edge(f"e{i}", "pdua", f"srv{i}") for i in range(1, 5)],
        edge("s1", "pdua", "sensor1"),
        edge("s2", "pdua", "sensor2"),
    ]
    return nodes, edges


# --- the exit criterion ------------------------------------------------------

def test_leaves_collapse_and_the_feeder_does_not():
    """The point of the whole exercise: fewer boxes, same chain."""
    nodes, _ = _rollup_by_rack(*build(), "power")
    ids = {n.id for n in nodes}
    assert "pdua" in ids, "the PDU was collapsed; the chain now skips a hop"
    assert "rppa" in ids
    assert not any(n.id.startswith("srv") for n in nodes)
    assert "rack:r1:server" in ids


def test_the_collapsed_node_says_how_many_it_stands_for():
    nodes, _ = _rollup_by_rack(*build(), "power")
    srv = next(n for n in nodes if n.id == "rack:r1:server")
    assert srv.rolled_up == 4
    assert srv.name == "R01"


def test_device_types_do_not_get_mixed_into_one_box():
    """Twenty servers and two sensors in a rack are two facts, not one."""
    nodes, _ = _rollup_by_rack(*build(), "power")
    ids = {n.id for n in nodes}
    assert {"rack:r1:server", "rack:r1:sensor"} <= ids


def test_power_adds_up_and_temperature_does_not():
    """A rack's draw is the sum of its loads; its inlet is the WORST one in it.
    An average hides the single hot server that is why anyone is looking."""
    nodes, _ = _rollup_by_rack(*build(), "power")
    srv = next(n for n in nodes if n.id == "rack:r1:server")
    assert srv.metrics["power_w"] == 1600.0
    assert srv.metrics["inlet_temp_c"] == 24.0


def test_the_edges_merge_and_carry_the_conductor_count():
    _, edges = _rollup_by_rack(*build(), "power")
    to_servers = [e for e in edges if e.target == "rack:r1:server"]
    assert len(to_servers) == 1
    assert to_servers[0].count == 4


def test_a_merged_edge_claims_no_termination():
    """Four cords land on four different outlets. Printing one of their labels
    on the merged line would be a lie with a socket number on it."""
    _, edges = _rollup_by_rack(*build(), "power")
    merged = next(e for e in edges if e.target == "rack:r1:server")
    assert merged.a_termination.id is None
    assert merged.b_termination.id is None


def test_a_group_of_one_is_left_alone():
    """Collapsing a single device hides its name behind a box that says '1
    server', which is strictly worse than the device."""
    nodes = [node("pdua", "pdu"), node("srv1", "server")]
    edges = [edge("e1", "pdua", "srv1")]
    kept, _ = _rollup_by_rack(nodes, edges, "power")
    assert {n.id for n in kept} == {"pdua", "srv1"}


def test_a_device_in_no_rack_is_never_collapsed():
    """Floor-standing plant - chillers, switchgear, UPS - has no rack, and it
    is exactly the equipment the power and cooling layers are about."""
    nodes = [node("chl1", "chiller", rack=LocationRef()),
             node("chl2", "chiller", rack=LocationRef())]
    kept, _ = _rollup_by_rack(nodes, [], "power")
    assert {n.id for n in kept} == {"chl1", "chl2"}


def test_the_rack_wears_its_unhappiest_member():
    nodes, edges = build()
    nodes[3].max_severity = "CRITICAL"       # srv2
    kept, _ = _rollup_by_rack(nodes, edges, "power")
    srv = next(n for n in kept if n.id == "rack:r1:server")
    assert srv.max_severity == "CRITICAL"


def test_one_dead_server_does_not_make_the_rack_offline():
    nodes, edges = build()
    nodes[2].status = "OFFLINE"              # srv1
    kept, _ = _rollup_by_rack(nodes, edges, "power")
    srv = next(n for n in kept if n.id == "rack:r1:server")
    assert srv.status == "ONLINE"
    assert srv.offline_count == 1


def test_a_rack_whose_every_member_is_dark_reads_offline():
    nodes, edges = build()
    for n in nodes[2:6]:
        n.status = "OFFLINE"
    kept, _ = _rollup_by_rack(nodes, edges, "power")
    srv = next(n for n in kept if n.id == "rack:r1:server")
    assert srv.status == "OFFLINE"
    assert srv.offline_count == 4


def test_a_link_internal_to_one_collapsed_group_is_dropped():
    """Both ends landed in the same box. Drawn as a self-loop it says nothing
    and it is the one edge shape the layout cannot place."""
    nodes = [node("srv1", "server"), node("srv2", "server"),
             node("srv3", "server")]
    edges = [edge("x", "srv1", "srv2")]
    # srv1 feeds srv2, so srv1 is not a leaf; srv2 and srv3 are.
    _, out = _rollup_by_rack(nodes, edges, "power")
    assert all(e.source != e.target for e in out)


def test_management_orients_the_other_way_round():
    """On the management layer the MANAGED device holds the a end, so 'feeds
    something' has to be read from the b column or every OOB switch collapses
    and every server survives - the exact inverse of the truth."""
    nodes = [node("oob1", "oob_switch"),
             *[node(f"srv{i}", "server") for i in range(1, 4)]]
    # a = the managed device, b = the switch (see core/layers).
    edges = [TopologyEdge(id=f"m{i}", source=f"srv{i}", target="oob1",
                          layer="management") for i in range(1, 4)]
    kept, _ = _rollup_by_rack(nodes, edges, "management")
    ids = {n.id for n in kept}
    assert "oob1" in ids, "the OOB switch was collapsed as if it were a leaf"
    assert "rack:r1:server" in ids
