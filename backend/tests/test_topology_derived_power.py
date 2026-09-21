"""What a device draws when it does not meter itself.

A remote power panel is a cabinet of breakers and an ASCO 7000 is a switch.
Neither meters unless it was ordered with metering, and this estate ordered
neither - the simulator says so where it models them. That is the hardware,
not a gap in the platform, but it left eight panels and two transfer switches
per datacenter drawing a blank on the canvas beside a Verdigris EV2 that was
reading 36 kW off the panel's own bus.

The reading is borrowed, never merged into `metrics`: a borrowed figure that
cannot be told from a measured one is the same mistake as a rack node printing
the first server's vendor for all twenty.
"""

from __future__ import annotations

from app.schemas import TopologyEdge, TopologyNode
from app.services.topology import _derive_power


def node(nid: str, name: str, dtype: str, power: float | None = None,
         rolled: int = 0) -> TopologyNode:
    metrics = {} if power is None else {"power_w": power}
    return TopologyNode(id=nid, name=name, device_type=dtype,
                        metrics=metrics, rolled_up=rolled)


def edge(src: str, dst: str) -> TopologyEdge:
    """A conductor runs from the feeder to the load."""
    return TopologyEdge(id=f"{src}->{dst}", source=src, target=dst, layer="power")


# ----------------------------------------------------------- the metered case

def test_a_panel_takes_the_reading_of_the_meter_on_its_bus():
    rpp = node("rpp", "RPPA-DC1-HA-R1-04", "rpp")
    ev2 = node("ev2", "EV21-DC1-HA-R1-04", "energy_monitor", 36204.0)
    pdu = node("pdu", "PDUA-DC1-HA-R2-01", "pdu", 8250.0)
    _derive_power([rpp, ev2, pdu], [edge("rpp", "ev2"), edge("rpp", "pdu")])

    assert rpp.derived_power_w == 36204.0
    assert rpp.derived_power_kind == "metered"
    assert rpp.derived_power_from == "EV21-DC1-HA-R1-04"
    # The borrowed figure stays out of the measurements.
    assert "power_w" not in rpp.metrics


def test_the_meter_outranks_the_sum_of_the_loads():
    """A measurement beats an inference even when both are available.

    The panel's PDUs add up to less than the panel draws - the difference is
    everything corded to it that the DCIM has no record of - and the meter is
    the one that saw it.
    """
    rpp = node("rpp", "RPPA", "rpp")
    ev2 = node("ev2", "EV21", "energy_monitor", 36204.0)
    pdu = node("pdu", "PDUA", "pdu", 8250.0)
    _derive_power([rpp, ev2, pdu], [edge("rpp", "ev2"), edge("rpp", "pdu")])
    assert rpp.derived_power_w == 36204.0


def test_a_meter_that_is_not_reading_is_not_a_reading():
    """It falls through to the loads rather than publishing the meter's silence."""
    rpp = node("rpp", "RPPA", "rpp")
    ev2 = node("ev2", "EV21", "energy_monitor")
    pdu = node("pdu", "PDUA", "pdu", 8250.0)
    _derive_power([rpp, ev2, pdu], [edge("rpp", "ev2"), edge("rpp", "pdu")])
    assert rpp.derived_power_w == 8250.0
    assert rpp.derived_power_kind == "downstream"


# -------------------------------------------------------- the downstream case

def test_a_transfer_switch_passes_what_its_loads_draw():
    """No meter is anywhere near an ATS, so the loads are the only answer."""
    ats = node("ats", "ATS1-DC1-UR", "ats")
    swgr = node("swgr", "SWGR1-DC1-UR", "switchgear", 190000.0)
    ups = node("ups", "UPSA-DC1-UR", "ups", 75000.0)
    mcc = node("mcc", "MCC1-DC1-MR", "mcc", 35000.0)
    _derive_power([ats, swgr, ups, mcc],
                  # Fed BY the switchgear, feeding the UPS and the MCC.
                  [edge("swgr", "ats"), edge("ats", "ups"), edge("ats", "mcc")])

    assert ats.derived_power_w == 110000.0
    assert ats.derived_power_kind == "downstream"
    assert ats.derived_power_from == "2 loads"


def test_one_silent_load_refuses_the_whole_sum():
    """A partial sum under-reports with nothing on screen to say that it does.

    Half of a transfer switch's throughput printed as its throughput is worse
    than a blank: it is a number somebody could size a maintenance window on.
    """
    ats = node("ats", "ATS1", "ats")
    ups = node("ups", "UPSA", "ups", 75000.0)
    mcc = node("mcc", "MCC1", "mcc")           # not reporting
    _derive_power([ats, ups, mcc], [edge("ats", "ups"), edge("ats", "mcc")])
    assert ats.derived_power_w is None


def test_the_meters_own_draw_is_not_counted_as_load():
    """An EV2 draws about 100 W of its own, which is not the panel's load."""
    rpp = node("rpp", "RPPA", "rpp")
    ev2 = node("ev2", "EV21", "energy_monitor")     # silent, so no metered path
    pdu = node("pdu", "PDUA", "pdu", 8250.0)
    _derive_power([rpp, ev2, pdu], [edge("rpp", "ev2"), edge("rpp", "pdu")])
    assert rpp.derived_power_w == 8250.0


# ------------------------------------------------------------- what is skipped

def test_a_device_that_meters_itself_is_left_alone():
    ups = node("ups", "UPSA", "ups", 75000.0)
    rpp = node("rpp", "RPPA", "rpp", 1.0)
    _derive_power([ups, rpp], [edge("ups", "rpp")])
    assert ups.derived_power_w is None


def test_a_rolled_up_node_is_left_alone():
    """A rack is not a device and has no bus for a meter to sit on."""
    rack = node("rack", "R2-05", "server", rolled=18)
    pdu = node("pdu", "PDUA", "pdu", 8250.0)
    _derive_power([rack, pdu], [edge("rack", "pdu")])
    assert rack.derived_power_w is None


def test_a_leaf_gets_nothing():
    """Nothing downstream, nothing to borrow. A blank is the honest answer."""
    ats = node("ats", "ATS1", "ats")
    swgr = node("swgr", "SWGR1", "switchgear", 190000.0)
    _derive_power([ats, swgr], [edge("swgr", "ats")])
    assert ats.derived_power_w is None


def test_the_direction_of_the_conductor_decides_which_way_it_reads():
    """Upstream is not downstream.

    Reading the feeder instead of the load would give every panel in a hall
    the UPS's whole output, which is the sort of number that looks plausible
    on a diagram and is wrong by a factor of four.
    """
    rpp = node("rpp", "RPPA", "rpp")
    ups = node("ups", "UPSA", "ups", 75000.0)
    _derive_power([rpp, ups], [edge("ups", "rpp")])
    assert rpp.derived_power_w is None
