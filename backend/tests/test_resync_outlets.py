"""The outlet resync moves a cord to the outlet the export names, and only
when it changed - see app/importer/resync_outlets.py for why the full
importer cannot."""

from app.importer.resync_outlets import plan_moves


def _edge(src, dst, outlet, psu):
    return {"layer": "power", "src": src, "dst": dst, "supply_node": src,
            "load_node": dst, "outlet": outlet, "psu": psu}


def test_a_moved_cord_is_planned_and_an_unmoved_one_is_not():
    device_id = {"pa": "P", "s1": "S1", "s2": "S2"}
    outlet_id = {("P", 3): "o3", ("P", 9): "o9"}
    psu_id = {("S1", 1): "ps1", ("S2", 1): "ps2"}
    current = {("P", "S1", "ps1"): ("c1", "o3"),      # was on 3, now on 9
               ("P", "S2", "ps2"): ("c2", "o3")}      # stays on 3
    moves, unmatched = plan_moves(
        [_edge("pa", "s1", 9, 1), _edge("pa", "s2", 3, 1)],
        device_id, outlet_id, psu_id, current)
    assert moves == [("c1", "o9")] and unmatched == []


def test_what_the_platform_does_not_hold_is_reported_not_guessed():
    moves, unmatched = plan_moves(
        [_edge("pa", "zz", 1, 1), _edge("pa", "s1", 40, 1), {"layer": "production"}],
        {"pa": "P", "s1": "S1"}, {("P", 1): "o1"}, {("S1", 1): "ps1"}, {})
    assert moves == [] and len(unmatched) == 2
